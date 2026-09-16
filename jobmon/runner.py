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

from . import config_loader, ledger, normalize, notify, profiles, scoring, store
from .adapters import ats_public, github_repos
from .analyze import keywords, sponsorship, variants
from .filters import eligibility, role
from .models import AdapterHealth, Analysis, Posting
from .profiles import Profile

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
    # addendum 2: one entry per profile delivered for. The totals above are their sum,
    # kept because the Actions log and the exit-code logic both read a single line.
    profiles: dict[str, object] = field(default_factory=dict)  # name -> ProfileResult

    def summary(self) -> str:
        return (
            f"fetched={self.fetched} new={self.new} duplicates={self.duplicates} "
            f"alerted={self.alerted} blocked={self.blocked} closed={self.closed} "
            f"seeded={self.seeded} "
            f"normalize_dropped={self.normalize_dropped} malformed={self.malformed} "
            f"retried={self.retried} adapters_failed={self.adapters_failed} "
            f"profiles={len(self.profiles)}"
        )


def run_scheduled(
    ledger_path: Path | None = None,
    health_path: Path = ledger.HEALTH_PATH,
    db_path: Path | None = None,
    dry_run: bool = False,
    seed: bool = False,
    profile_list: list[Profile] | None = None,
) -> RunResult:
    """One scheduled pass, with the committed ledger as the only durable state (step 6).

    Order: load ledger -> rebuild cache -> fetch -> diff -> **notify** -> write ledger.

    NOTIFY BEFORE WRITE, always. Writing the ledger before a failed notify loses that
    alert permanently; a failed write after a successful notify costs one duplicate next
    run. Bias toward duplicates - so the write is the last thing that happens, and if
    `notify` raises, the ledger on disk is untouched (T6.3).

    The caller (the workflow) commits the ledger after this returns. A failed commit is
    also survivable for the same reason: the next run re-alerts, and reports how many.

    `ledger_path` may name the state directory, `postings.jsonl`, or the pre-addendum
    `seen.jsonl`; all three resolve to the same directory, and a lone `seen.jsonl` is
    migrated into the split files on the way in (addendum 2, build step 1).
    """
    state_dir = ledger.resolve_state_dir(ledger_path)
    state = ledger.load_state(state_dir)
    previous_health = ledger.load_health(health_path)
    # Which postings had never been delivered to anyone before this run started. Used
    # below to count re-deliveries; captured now because the cache is about to be written.
    undelivered_before = {
        gid for gid in state.entries
        if not any(gid in by_id for by_id in state.alerts.values())
    }

    with tempfile.TemporaryDirectory(prefix="jobmon-cache-") as tmp:
        db = db_path or Path(tmp) / "cache.db"
        conn = store.connect(db)
        try:
            store.init_db(conn)
            restored = ledger.rebuild(conn, state)
            log.info("ledger: %d entries restored into the derived cache", restored)

            run_id = ledger.next_run_id(state.entries, previous_health)
            result = run(
                conn, dry_run=dry_run, seed=seed, run_id=run_id, profile_list=profile_list
            )

            # Postings carried over from the ledger that were never delivered: either a
            # notify failed, or the ledger write/commit after one did (T6.4).
            result.retried = sum(1 for gid in result.alerted_ids if gid in undelivered_before)

            current = {h.adapter: h.postings_seen for h in result.health}
            result.canaries = ledger.check_canary(previous_health, current)

            if not dry_run:
                # Only now, after delivery, is the durable state allowed to move.
                ledger.save_state(ledger.state_from_db(conn), state_dir)
                ledger.save_health(
                    ledger.Health(counts=current, last_run_id=run_id), health_path
                )
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
    profile_list: list[Profile] | None = None,
) -> RunResult:
    """Execute one full pass.

      1. Load config; open one shared httpx client.
      2. For each enabled adapter: fetch, normalize, upsert. Every exception is caught at
         that boundary - one broken adapter never kills the run - and an `AdapterHealth`
         row is recorded either way, including on a clean zero-result return.
      3. Analyze every posting that is new or has no analysis row. ONCE - analysis is
         objective and user-agnostic (addendum 2).
      4. For each profile: score, route, cap, sort, send.
      5. `mark_alerted` only after delivery is confirmed, per profile.

    `profile_list` defaults to every profile in `config/profiles/`, or to the built-in
    default profile when that directory is empty - which reproduces the pre-addendum
    behaviour exactly.

    `dry_run` does everything except send and except recording the alert.

    `seed` stores and marks everything alerted WITHOUT sending. Use it once, on a fresh
    database: the first pass finds every currently-open posting, and on a real board set
    that is over 13,000 notifications. Nothing else in the system prevents that.
    """
    result = RunResult()
    cfg = config_loader.load()
    tier = config_loader.tier_companies(cfg)
    profile_list = profile_list if profile_list is not None else profiles.select()
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

        # One ANALYSIS pass, then one loop per profile. Analysis is objective and is
        # shared; the profile loop below never touches a posting or analysis record
        # (addendum 2). Adding a second user therefore costs zero extra fetching and zero
        # extra analysis, and editing your own profile never triggers a re-crawl.
        cutoff = datetime.now(UTC) - RECENT_OPENING_WINDOW
        for profile in profile_list:
            # Driven by "no alert row for THIS profile", not by the upsert saying "new". A
            # posting whose delivery failed last run is stored and no longer new; keying
            # off the outcome would drop it forever instead of retrying it. Scoped per
            # profile so one profile's alert cannot silence another's (T8.1).
            pending = store.unalerted_postings(conn, run_id, profile.name)
            pending = [
                posting for posting in pending
                if (posting.posted_at or posting.first_seen_at) >= cutoff
            ]
            outcome = _deliver_profile(
                client, conn, profile, pending, cfg, tier, dry_run=dry_run, seed=seed
            )
            result.profiles[profile.name] = outcome
            result.alerted += outcome.alerted
            result.blocked += outcome.blocked
            result.closed += outcome.closed
            result.seeded += outcome.seeded
            result.alerted_ids.extend(outcome.alerted_ids)
    return result


@dataclass(slots=True)
class ProfileResult:
    """What one profile's pass did. `jobmon run` prints one of these per profile."""

    profile: str
    considered: int = 0
    instant: int = 0
    digest: int = 0
    silent: int = 0
    alerted: int = 0
    blocked: int = 0
    closed: int = 0
    seeded: int = 0
    truncated_digest: int = 0
    held_by_company: int = 0
    alerted_ids: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"[{self.profile}] considered={self.considered} instant={self.instant} "
            f"digest={self.digest} silent={self.silent} alerted={self.alerted} "
            f"blocked={self.blocked} truncated={self.truncated_digest} "
            f"company_held={self.held_by_company}"
        )


def _deliver_profile(
    client,
    conn: sqlite3.Connection,
    profile: Profile,
    pending: list[Posting],
    cfg,
    tier: set[str],
    dry_run: bool = False,
    seed: bool = False,
) -> ProfileResult:
    """Score, route, cap, sort and send one profile's share of this run.

    Delivery order within the run is instants first, then the digest, because an instant
    that arrives after the digest listing the same posting is just a duplicate.
    """
    out = ProfileResult(profile=profile.name, considered=len(pending))
    applied = store.applied_ids(conn) if profile.limits.suppress_if_applied else set()
    reposts = store.reposted_ids(conn) if profile.limits.suppress_reposts else set()

    candidates: list[scoring.Candidate] = []
    for posting in pending:
        analysis = _analyze(conn, posting, cfg)
        # Objective suppressions, ahead of any profile opinion: a row the SOURCE marked
        # closed is not a job, and a repost was already delivered under this global_id.
        if posting.raw.get("closed"):
            out.closed += 1
            continue
        if posting.global_id in reposts:
            continue
        candidate = scoring.route(posting, analysis, profile, tier, applied)
        if candidate.route == "silent" and analysis.eligibility == "blocked":
            out.blocked += 1
        candidates.append(candidate)

    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    capped = scoring.apply_caps(
        candidates,
        profile,
        already_today={
            "instant": store.count_alerts_since(
                conn, day_start, profile.name, channel="instant"
            ),
        },
        already_by_company=store.alerts_by_company_since(conn, day_start, profile.name),
    )
    out.instant = len(capped.instant)
    out.digest = len(capped.digest)
    out.silent = len(capped.silent)
    out.truncated_digest = capped.truncated_digest
    out.held_by_company = sum(capped.held_by_company.values())

    ordered_instant = scoring.sort_candidates(capped.instant, profile)
    ordered_digest = scoring.sort_candidates(capped.digest, profile)

    if seed:
        # Store everything as already-alerted so the next run only reports genuine
        # arrivals. Nothing is sent.
        sendable = [*ordered_instant, *ordered_digest]
        store.mark_alerted(
            conn, [c.global_id for c in sendable], datetime.now(UTC),
            profile=profile.name, channel="seed",
            scores={c.global_id: c.score for c in sendable},
        )
        out.seeded = len(sendable)
        return out

    if dry_run:
        for candidate in [*ordered_instant, *ordered_digest]:
            print(f"--- [{profile.name}] {candidate.route} "
                  f"score={candidate.score:.2f} group={candidate.group}")
            print(notify.format_alert(
                candidate.posting, candidate.analysis, profile, candidate.score
            ))
        return out

    now = datetime.now(UTC)
    for candidate in ordered_instant:
        if notify.send_instant(client, candidate, profile):
            store.mark_alerted(conn, [candidate.global_id], now, profile=profile.name,
                               channel="instant",
                               scores={candidate.global_id: candidate.score})
            out.alerted_ids.append(candidate.global_id)
    if notify.send_profile_digest(
        client, ordered_digest, profile, capped.truncated_digest, capped.held_by_company
    ):
        # `mark_alerted` runs only for what was actually delivered. A failed send leaves
        # no alert row, so the posting is retried next run rather than lost - one
        # duplicate costs less than one missed posting.
        ids = [c.global_id for c in ordered_digest]
        store.mark_alerted(conn, ids, now, profile=profile.name, channel="digest",
                           scores={c.global_id: c.score for c in ordered_digest})
        out.alerted_ids.extend(ids)
    out.alerted = len(out.alerted_ids)
    return out


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
    # Addendum 4: employer filing history folded in, AFTER the text classifier and only
    # ever in the `unknown` direction. `text_label` is kept so the promotion is auditable.
    combined, confidence, sponsor_evidence = sponsorship.combine(
        label, posting.company, cfg.sponsor_history
    )
    blocking_groups = sorted({
        group for sentence in evidence
        if (group := eligibility.blocking_group(sentence)) is not None
    })
    matched = keywords.match(text, posting.title, cfg.taxonomy)
    gaps = keywords.gap_list(matched, cfg.skill_inventory)
    scores = variants.score(matched, cfg.variants)
    return _persist(
        conn,
        Analysis(
            global_id=posting.global_id,
            role_class=role.classify_role(posting.title, text),
            function=role.classify_function(posting.title, text),
            eligibility=combined,
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
            text_eligibility=label,
            sponsor_confidence=confidence,
            sponsor_evidence=sponsor_evidence,
            blocking_groups=blocking_groups,
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
