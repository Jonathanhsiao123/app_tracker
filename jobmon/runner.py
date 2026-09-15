"""One pass of the pipeline (spec 3). PHASE 1.

    [source adapters] -> [normalizer] -> [dedup store] -> [filters] -> [analyzer] -> [notifier]

Single process, run hourly so recent openings reach the batch promptly.

Build steps 3, 4, 5, 6 and 7a/7b are wired in: fetch, normalize, dedup, role filter,
eligibility, keywords, gap list, variant scoring, the committed ledger that
`run_scheduled` treats as the only durable state, and the curated-repo adapter. Build step
7c (`adapters/bigtech/`) is not - build only if time remains under the day-7 timebox.

`monitor.yml` runs `run_scheduled` unattended. It reaches you on the ntfy topic and stays
green for anything it managed to push there - see the exit codes in `cli.py`.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config_loader, ledger, normalize, notify, store
from .adapters import ats_public, github_repos
from .analyze import keywords, variants
from .filters import eligibility, role
from .models import AdapterHealth, Analysis, Posting

log = logging.getLogger(__name__)

# Outcomes from store.upsert_posting that may alert. Everything else was seen before.
ALERTABLE_OUTCOMES = frozenset({"new"})

# Share of a board sweep that may fail to parse before it stops being noise and starts
# being an ATS that changed its schema. Below this, malformed postings are counted and
# logged; above it, the adapter is reported as unhealthy and the problem is pushed.
MALFORMED_ALERT_RATIO = 0.05
RECENT_OPENING_WINDOW = timedelta(hours=24)


@dataclass(slots=True)
class RunResult:
    """What one pass did. Printed by `jobmon run` and folded into the weekly summary."""

    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    alerted: int = 0
    blocked: int = 0
    closed: int = 0  # github_repo rows the source itself marked closed; stored, not alerted
    adapters_failed: int = 0
    normalize_dropped: int = 0
    malformed: int = 0  # raised on the way into the store; skipped, not fatal
    seeded: int = 0
    retried: int = 0  # delivered this run after a previous run failed to deliver them
    health: list[AdapterHealth] = field(default_factory=list)
    alerted_ids: list[str] = field(default_factory=list)
    canaries: list[object] = field(default_factory=list)  # ledger.Canary

    def summary(self) -> str:
        return (
            f"fetched={self.fetched} new={self.new} duplicates={self.duplicates} "
            f"alerted={self.alerted} blocked={self.blocked} closed={self.closed} "
            f"seeded={self.seeded} "
            f"normalize_dropped={self.normalize_dropped} malformed={self.malformed} "
            f"retried={self.retried} adapters_failed={self.adapters_failed}"
        )


def run_scheduled(
    ledger_path: Path = ledger.LEDGER_PATH,
    health_path: Path = ledger.HEALTH_PATH,
    db_path: Path | None = None,
    dry_run: bool = False,
    seed: bool = False,
) -> RunResult:
    """One scheduled pass, with the committed ledger as the only durable state (step 6).

    Order: load ledger -> rebuild cache -> fetch -> diff -> **notify** -> write ledger.

    NOTIFY BEFORE WRITE, always. Writing the ledger before a failed notify loses that
    alert permanently; a failed write after a successful notify costs one duplicate next
    run. Bias toward duplicates - so the write is the last thing that happens, and if
    `notify` raises, the ledger on disk is untouched (T6.3).

    The caller (the workflow) commits the ledger after this returns. A failed commit is
    also survivable for the same reason: the next run re-alerts, and reports how many.
    """
    entries = ledger.load(ledger_path)
    previous_health = ledger.load_health(health_path)

    with tempfile.TemporaryDirectory(prefix="jobmon-cache-") as tmp:
        db = db_path or Path(tmp) / "cache.db"
        conn = store.connect(db)
        try:
            store.init_db(conn)
            restored = ledger.rebuild(conn, entries)
            log.info("ledger: %d entries restored into the derived cache", restored)

            run_id = ledger.next_run_id(entries, previous_health)
            result = run(conn, dry_run=dry_run, seed=seed, run_id=run_id)

            # Postings carried over from the ledger that were never delivered: either a
            # notify failed, or the ledger write/commit after one did (T6.4).
            result.retried = sum(
                1
                for gid in result.alerted_ids
                if gid in entries and entries[gid].alerted_at is None
            )

            current = {h.adapter: h.postings_seen for h in result.health}
            result.canaries = ledger.check_canary(previous_health, current)

            if not dry_run:
                # Only now, after delivery, is the durable state allowed to move.
                ledger.save(ledger.from_db(conn), ledger_path)
                ledger.save_health(ledger.Health(counts=current, last_run_id=run_id), health_path)
        finally:
            conn.close()
    return result


def enabled_adapters() -> list[object]:
    """The adapter modules to sweep this run.

    A plain list, built by hand (spec 12: no plugin discovery). `bigtech/` adds to this list
    only if build step 7c ships under the timebox.
    """
    return [ats_public, github_repos]


def run(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    seed: bool = False,
    adapter: str | None = None,
    run_id: int | None = None,
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
    if run_id is None:
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
            seen = failed = dropped = malformed = 0
            error: str | None = None
            first_bad: str | None = None
            try:
                for raw in module.fetch(client, cfg.companies):
                    seen += 1
                    # PER-POSTING isolation, inside the per-adapter one. These two lines
                    # used to sit in the adapter-wide try below, which made a single
                    # malformed posting out of 13,000 abort the whole sweep: every board
                    # after it was dropped, the adapter was recorded as failed, and the
                    # run exited non-zero. In Phase 1 `enabled_adapters()` returns exactly
                    # one module, so "the adapter failed" and "the run delivered nothing"
                    # are the same sentence - the independence the comment below promises
                    # only exists between adapters, and there is only one.
                    #
                    # A posting that will not parse is data, not a broken pipeline. Count
                    # it, keep the other 12,999.
                    try:
                        posting = normalize.normalize(raw)
                        if posting is None:
                            dropped += 1
                            continue
                        outcome = store.upsert_posting(conn, posting, run_id)
                    except Exception as exc:  # deliberate; see above
                        malformed += 1
                        if first_bad is None:
                            ident = (raw.get("id") or raw.get("shortcode")
                                      or raw.get("apply_url") or "?")
                            first_bad = (f"{raw.get('_company', '?')} {ident}: "
                                         f"{type(exc).__name__}: {exc}")
                            log.exception("posting %s could not be stored", ident)
                        continue
                    if outcome == "new":
                        result.new += 1
                    elif outcome == "duplicate":
                        result.duplicates += 1
            except Exception as exc:  # spec 5b: adapters fail loudly and independently
                error = f"{type(exc).__name__}: {exc}"
                failed = 1
                log.exception("adapter %s failed", name)
            # A few unparseable postings are ordinary internet. A large fraction is a
            # schema change at one of the ATSes, which is worth a push - and is invisible
            # to the zero-result canary, because the raw postings still arrive.
            if malformed and not failed and malformed > seen * MALFORMED_ALERT_RATIO:
                error = (f"{malformed} of {seen} postings could not be stored "
                         f"({malformed / seen:.0%}); first: {first_bad}")
            result.fetched += seen
            result.malformed += malformed
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
        cutoff = datetime.now(UTC) - RECENT_OPENING_WINDOW
        pending = [
            posting for posting in pending
            if (posting.posted_at or posting.first_seen_at) >= cutoff
        ]
        sendable: list[tuple[Posting, Analysis]] = []
        for posting in pending:
            analysis = _analyze(conn, posting, cfg)
            ok, reason = notify.should_alert(posting, analysis, tier)
            if not ok:
                if reason == "blocked":
                    result.blocked += 1
                elif reason == "closed":
                    result.closed += 1
                continue
            sendable.append((posting, analysis))

        if seed:
            # Store everything as already-alerted so the next run only reports genuine
            # arrivals. Nothing is sent.
            store.mark_alerted(conn, [p.global_id for p, _ in sendable], datetime.now(UTC))
            result.seeded = len(sendable)
            result.alerted_ids = []
            return result

        if dry_run:
            for posting, analysis in sendable:
                print(notify.format_alert(posting, analysis))
                print("-" * 60)
            result.alerted = 0
            return result

        result.alerted_ids = _send(client, conn, sendable, tier)
        result.alerted = len(result.alerted_ids)
    return result


def _send(
    client, conn: sqlite3.Connection, sendable: list[tuple[Posting, Analysis]], tier: set[str]
) -> list[str]:
    """Send every eligible opening in one batch, ordered newest-first.

    Returns the global_ids actually delivered.

    `mark_alerted` runs only for what was actually delivered. A failed send leaves
    `alerted_at` unset so the posting is retried next run rather than lost - one duplicate
    costs less than one missed posting.
    """
    delivered: list[str] = []
    if notify.send_digest(client, sendable, "jobmon digest"):
        delivered.extend(p.global_id for p, _ in sendable)
    if delivered:
        store.mark_alerted(conn, delivered, datetime.now(UTC))
    return delivered


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
