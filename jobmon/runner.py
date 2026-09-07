"""One pass of the pipeline (spec 3). PHASE 1.

    [source adapters] -> [normalizer] -> [dedup store] -> [filters] -> [analyzer] -> [notifier]

Single process, run on a schedule. Every 3 hours - faster is not useful, recruiters do not
read a queue in real time.

Build steps 3, 4 and 5 are wired in: fetch, normalize, dedup, role filter, eligibility,
keywords, gap list and variant scoring. Build steps 6 (committed ledger) and 7 (tracking,
curated-repo adapter) are not - `monitor.yml` still has placeholder persistence, so this
must not run unattended yet.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import config_loader, normalize, notify, store
from .adapters import ats_public
from .analyze import keywords, variants
from .filters import eligibility, role
from .models import AdapterHealth, Analysis, Posting

log = logging.getLogger(__name__)

# Outcomes from store.upsert_posting that may alert. Everything else was seen before.
ALERTABLE_OUTCOMES = frozenset({"new"})


@dataclass(slots=True)
class RunResult:
    """What one pass did. Printed by `jobmon run` and folded into the weekly summary."""

    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    alerted: int = 0
    blocked: int = 0
    adapters_failed: int = 0
    normalize_dropped: int = 0
    seeded: int = 0
    health: list[AdapterHealth] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"fetched={self.fetched} new={self.new} duplicates={self.duplicates} "
            f"alerted={self.alerted} blocked={self.blocked} seeded={self.seeded} "
            f"normalize_dropped={self.normalize_dropped} adapters_failed={self.adapters_failed}"
        )


def enabled_adapters() -> list[object]:
    """The adapter modules to sweep this run.

    A plain list, built by hand. Phase 1 = [ats_public]. Phase 3 adds github_repos and
    whichever `bigtech` modules are enabled. No plugin discovery (spec 12).
    """
    return [ats_public]


def run(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    seed: bool = False,
    adapter: str | None = None,
) -> RunResult:
    """Execute one full pass.

      1. Load config; open one shared httpx client.
      2. For each enabled adapter: fetch, normalize, upsert. Every exception is caught at
         that boundary - one broken adapter never kills the run - and an `AdapterHealth`
         row is recorded either way, including on a clean zero-result return.
      3. Analyze every posting that is new or has no analysis row.
      4. Partition into tier-1 instant alerts and the batched digest; send.
      5. `mark_alerted` only after delivery is confirmed.

    `dry_run` does everything except send and except writing `alerted_at`.

    `seed` stores and marks everything alerted WITHOUT sending. Use it once, on a fresh
    database: the first pass finds every currently-open posting, and on a real board set
    that is over 13,000 notifications. Nothing else in the system prevents that.
    """
    result = RunResult()
    cfg = config_loader.load()
    tier = config_loader.tier_companies(cfg)
    run_id = store.begin_run(conn)

    with ats_public.make_client() as client:
        for module in enabled_adapters():
            # Not `getattr(module, "name", module.__name__)`: the default is evaluated
            # eagerly, so an adapter without `name` would crash here - before the health
            # row that is supposed to record the failure gets written.
            name = getattr(module, "name", None) or getattr(
                module, "__name__", type(module).__name__
            )
            if adapter and name != adapter:
                continue
            started = time.monotonic()
            seen = failed = dropped = 0
            error: str | None = None
            try:
                for raw in module.fetch(client, cfg.companies):
                    seen += 1
                    posting = normalize.normalize(raw)
                    if posting is None:
                        dropped += 1
                        continue
                    outcome = store.upsert_posting(conn, posting, run_id)
                    if outcome == "new":
                        result.new += 1
                    elif outcome == "duplicate":
                        result.duplicates += 1
            except Exception as exc:  # spec 5b: adapters fail loudly and independently
                error = f"{type(exc).__name__}: {exc}"
                failed = 1
                log.exception("adapter %s failed", name)
            result.fetched += seen
            result.normalize_dropped += dropped
            result.adapters_failed += failed
            health = AdapterHealth(
                adapter=name,
                run_at=datetime.now(UTC),
                ok=not failed,
                postings_seen=seen,
                new_postings=result.new,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=error,
            )
            store.record_health(conn, health)
            result.health.append(health)

        # Alerting is driven by `alerted_at IS NULL`, NOT by the upsert saying "new". A
        # posting whose delivery failed last run is stored and no longer new; keying off
        # the outcome would drop it forever instead of retrying it.
        pending = store.unalerted_postings(conn, run_id)
        sendable: list[tuple[Posting, Analysis]] = []
        for posting in pending:
            analysis = _analyze(conn, posting, cfg)
            ok, reason = notify.should_alert(posting, analysis, tier)
            if not ok:
                if reason == "blocked":
                    result.blocked += 1
                continue
            sendable.append((posting, analysis))

        if seed:
            # Store everything as already-alerted so the next run only reports genuine
            # arrivals. Nothing is sent.
            store.mark_alerted(conn, [p.global_id for p, _ in sendable], datetime.now(UTC))
            result.seeded = len(sendable)
            return result

        if dry_run:
            for posting, analysis in sendable:
                print(notify.format_alert(posting, analysis))
                print("-" * 60)
            result.alerted = 0
            return result

        result.alerted = _send(client, conn, sendable, tier)
    return result


def _send(
    client, conn: sqlite3.Connection, sendable: list[tuple[Posting, Analysis]], tier: set[str]
) -> int:
    """Tier-1 postings alert individually; everything else goes out as one digest.

    `mark_alerted` runs only for what was actually delivered. A failed send leaves
    `alerted_at` unset so the posting is retried next run rather than lost - one duplicate
    costs less than one missed posting.
    """
    delivered: list[str] = []
    digest: list[tuple[Posting, Analysis]] = []
    for posting, analysis in sendable:
        if posting.company in tier:
            if notify.send_alert(client, posting, analysis):
                delivered.append(posting.global_id)
        else:
            digest.append((posting, analysis))
    if digest and notify.send_digest(client, digest, "jobmon digest"):
        delivered.extend(p.global_id for p, _ in digest)
    if delivered:
        store.mark_alerted(conn, delivered, datetime.now(UTC))
    return len(delivered)


def _analyze(conn: sqlite3.Connection, posting: Posting, cfg) -> Analysis:
    """Build and persist the Analysis row for one posting.

    Everything in the Analysis record is now real: role class, function, grad window, YoE
    floor, degree gating, eligibility, keywords, gaps and variant scores.

    `gaps` is stored as `list[str]` to match spec 4, with cosmetic gaps rendered as
    "tensorflow (~pytorch)" so the real/cosmetic distinction survives into the alert.
    `keywords.gap_list` returns the structured form for reports that need to filter on it.
    """
    text = posting.description_text
    label, evidence = eligibility.classify(text, posting.company, cfg.sponsorship_overrides)
    matched = keywords.match(text, posting.title, cfg.taxonomy)
    gaps = keywords.gap_list(matched, cfg.skill_inventory)
    scores = variants.score(matched, cfg.variants)
    return _persist(
        conn,
        Analysis(
            global_id=posting.global_id,
            role_class=role.classify_role(posting.title, text),
            function=role.classify_function(posting.title, text),
            eligibility=label,
            eligibility_evidence=evidence,
            keywords=matched,
            gaps=[
                g["term"] if g["type"] == "real" else f"{g['term']} (~{g['substitute']})"
                for g in sorted(gaps, key=lambda g: (g["type"] != "real", g["term"]))
            ],
            recommended_variant=variants.recommend(scores),
            variant_scores=scores,
            grad_window=role.extract_grad_window(text),
            degree_gated=role.is_degree_gated(text),
            yoe_floor=role.extract_yoe_floor(text),
        ),
    )


def _persist(conn: sqlite3.Connection, analysis: Analysis) -> Analysis:
    store.upsert_analysis(conn, analysis)
    return analysis


def analyze_pending(conn: sqlite3.Connection, reanalyze_all: bool = False) -> int:
    """Analyze postings with no analysis row. Returns the count.

    `reanalyze_all` re-runs everything after a taxonomy or override edit. It overwrites the
    analysis in place and never touches `alerted_at`, so it cannot cause a re-alert.
    """
    cfg = config_loader.load()
    sql = "SELECT p.global_id FROM postings p"
    if not reanalyze_all:
        sql += " LEFT JOIN analysis a ON a.global_id = p.global_id WHERE a.global_id IS NULL"
    count = 0
    for row in conn.execute(sql).fetchall():
        posting = store.get_posting(conn, row["global_id"])
        if posting is not None:
            _analyze(conn, posting, cfg)
            count += 1
    return count
