"""CLI entry point (spec 3, 11). CLI output plus push notifications is the whole interface -
there is no dashboard and there will not be one (spec 2).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import audit, feedback, ledger, notify, profiles, runner, scoring, store
from .adapters import ats_public

SINCE_UNITS = {"h": "hours", "d": "days", "w": "weeks"}

# Exit codes, and the reason there is more than one kind of non-zero.
#
# A red Actions run emails you. An email saying "jobmon failed" looks exactly like an email
# saying "jobmon failed" whether the cause is a dead adapter or a crash, and neither is the
# thing you asked to be told about - postings are. So a run that completed and already
# pushed the problem to your phone exits PIPELINE_REPORTED, which `monitor.yml` treats as
# green. Only a genuine crash, or a misconfiguration that leaves NO channel to reach you
# (EXIT_MISCONFIGURED), is allowed to turn the run red and mail you.
EXIT_OK = 0
EXIT_MISCONFIGURED = 2  # nothing ran, and push notification is not available to say so
EXIT_PIPELINE_REPORTED = 3  # the run finished; the problem went out on the ntfy topic


def build_parser() -> argparse.ArgumentParser:
    """Wire up the subcommands. Flat, `argparse` only, no click/typer."""
    parser = argparse.ArgumentParser(prog="jobmon", description="Job posting monitor.")
    parser.add_argument("--db", default=str(store.DB_PATH), help="SQLite path")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the SQLite file")

    run_cmd = sub.add_parser("run", help="one pipeline pass")
    run_cmd.add_argument("--dry-run", action="store_true",
                         help="fetch, store and analyze, but print alerts instead of sending")
    run_cmd.add_argument("--seed", action="store_true",
                         help="mark everything found as already alerted WITHOUT sending; "
                              "use once on a fresh database, or the first run notifies you "
                              "about every currently-open posting")
    run_cmd.add_argument("--adapter", help="restrict the sweep to one adapter")
    run_cmd.add_argument("--ledger", type=Path, default=None,
                         help="use the committed ledger at this path as the only durable "
                              "state, rebuilding the SQLite cache in a tmpdir (build "
                              "step 6). This is the mode the cron workflow runs.")
    run_cmd.add_argument(
        "--min-interval", default=None, metavar="DURATION",
        help="skip the sweep entirely if the previous run finished less than this ago "
             "(e.g. 50m, 2h). Lets the workflow fire several staggered cron attempts per "
             "hour - GitHub drops scheduled runs under load - while still delivering at "
             "most one digest per hour. Requires --ledger. Exits 0 when it skips.",
    )
    run_cmd.add_argument("--profile", help="deliver for one profile only; default is "
                                           "every profile in config/profiles/")

    list_cmd = sub.add_parser("list", help="stored postings, newest first")
    list_cmd.add_argument("--since", default="24h", help="e.g. 24h, 3d, 2w")
    list_cmd.add_argument("--show-blocked", action="store_true",
                          help="include postings suppressed as ineligible")
    list_cmd.add_argument("--company")
    list_cmd.add_argument("--function", help="filter to one function, e.g. mle")
    list_cmd.add_argument("--profile", help="score and sort against this profile")
    list_cmd.add_argument("--all", action="store_true", dest="show_all",
                          help="include postings this profile routed to silent - the caps "
                               "and thresholds change what reaches you, never what is stored")
    list_cmd.add_argument("--resort", action="store_true",
                          help="re-rank the stored postings against the CURRENT profile. "
                               "Makes zero network calls: edit priorities.order, re-run "
                               "this, see the effect in a second (Phase X)")

    audit_cmd = sub.add_parser(
        "audit", help="what was actually sent, and which lever would cut it (addendum 1)")
    audit_cmd.add_argument("--days", type=int, default=7)
    audit_cmd.add_argument("--profile", help="restrict to one profile")

    profiles_cmd = sub.add_parser("profiles", help="loaded profiles and their resolved weights")
    profiles_cmd.add_argument("--profile", help="show one in full")

    mute_cmd = sub.add_parser("mute", help="never alert me about this posting again")
    mute_cmd.add_argument("global_id")
    mute_cmd.add_argument("--profile", required=True)
    mute_cmd.add_argument("--reason", choices=list(feedback.MUTE_REASONS),
                          help="what was wrong with it; `jobmon tune` reads these")

    mute_co = sub.add_parser("mute-company", help="never alert me about this employer again")
    mute_co.add_argument("name")
    mute_co.add_argument("--profile", required=True)
    mute_co.add_argument("--reason", choices=list(feedback.MUTE_REASONS))

    boost_cmd = sub.add_parser("boost", help="rank this posting higher")
    boost_cmd.add_argument("global_id")
    boost_cmd.add_argument("--profile", required=True)

    tune_cmd = sub.add_parser(
        "tune", help="propose a profile edit from your mutes. Proposes only - never edits")
    tune_cmd.add_argument("--profile", required=True)

    show_cmd = sub.add_parser("show", help="full record, analysis, evidence sentences")
    show_cmd.add_argument("global_id")

    apply_cmd = sub.add_parser("apply", help="mark that YOU applied (spec 1, 11)")
    apply_cmd.add_argument("global_id")
    apply_cmd.add_argument("--variant", required=True)
    apply_cmd.add_argument("--note")

    log_cmd = sub.add_parser("log", help="advance the funnel stage")
    log_cmd.add_argument("global_id")
    log_cmd.add_argument("--status", required=True,
                         choices=["screen", "reject", "onsite", "offer"])
    log_cmd.add_argument("--note")

    sub.add_parser("funnel", help="conversion by variant and function")
    sub.add_parser("health", help="per-adapter last-run counts and failures")

    re_cmd = sub.add_parser("reanalyze", help="re-run the analyzer after a config edit")
    re_cmd.add_argument("--all", action="store_true", help="re-analyze every posting")
    return parser


def parse_since(value: str) -> datetime:
    """Parse "24h" / "3d" / "2w" into a UTC-aware datetime."""
    m = re.fullmatch(r"(\d+)\s*([hdw])", value.strip(), re.IGNORECASE)
    if not m:
        raise argparse.ArgumentTypeError(f"cannot parse --since {value!r}; use 24h, 3d or 2w")
    amount, unit = int(m.group(1)), m.group(2).lower()
    return datetime.now(UTC) - timedelta(**{SINCE_UNITS[unit]: amount})


DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(value: str) -> timedelta:
    """Parse "50m" / "2h" / "1d" into a timedelta."""
    m = re.fullmatch(r"(\d+)\s*([mhd])", value.strip(), re.IGNORECASE)
    if not m:
        raise argparse.ArgumentTypeError(
            f"cannot parse duration {value!r}; use e.g. 50m, 2h or 1d"
        )
    return timedelta(**{DURATION_UNITS[m.group(2).lower()]: int(m.group(1))})


def _since_last_run(health: ledger.Health) -> timedelta | None:
    """How long since the previous run finished, or None if that is not knowable."""
    if not health.last_run_at:
        return None
    try:
        before = datetime.fromisoformat(health.last_run_at)
    except ValueError:
        return None
    if before.tzinfo is None:
        before = before.replace(tzinfo=UTC)
    return datetime.now(UTC) - before


def cmd_run(args: argparse.Namespace) -> int:
    """`jobmon run`. Prints the RunResult summary and every adapter health line - this is
    what the GitHub Actions log shows, and the only place silent breakage becomes visible
    without opening the DB."""
    if args.dry_run and args.seed:
        print("--dry-run and --seed are mutually exclusive", file=sys.stderr)
        return EXIT_MISCONFIGURED
    if not args.dry_run and not args.seed and not os.environ.get("JOBMON_NTFY_TOPIC"):
        # Checked HERE, before the sweep, and not left to `_post_ntfy` at delivery time.
        # That refusal is right, but it fires two minutes in, after 169 boards have been
        # fetched, and it surfaces as a traceback - so the only thing you are told is
        # "workflow failed", not "you never set the secret". Same refusal, said up front
        # and in words.
        print(
            "JOBMON_NTFY_TOPIC is unset; refusing to run a sweep it cannot deliver.",
            "  Set it as a repository secret so the cron workflow can send:",
            "    Settings > Secrets and variables > Actions > New repository secret",
            "    (or: gh secret set JOBMON_NTFY_TOPIC)",
            "  The topic is any hard-to-guess string; subscribe to it in the ntfy app.",
            "  Use --dry-run to print alerts instead of sending them.",
            sep="\n",
            file=sys.stderr,
        )
        return EXIT_MISCONFIGURED
    try:
        chosen = profiles.select(only=args.profile)
    except profiles.ProfileError as exc:
        # T8.12: a bad profile stops the run, loudly, before a single board is fetched.
        # It must never degrade into "alert everything" - that is the flood this whole
        # layer exists to prevent.
        print(f"profile config error: {exc}", file=sys.stderr)
        return EXIT_MISCONFIGURED
    if args.min_interval is not None:
        if args.ledger is None:
            print("--min-interval requires --ledger (it reads the run stamp beside it)",
                  file=sys.stderr)
            return EXIT_MISCONFIGURED
        try:
            interval = parse_duration(args.min_interval)
        except argparse.ArgumentTypeError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_MISCONFIGURED
        health = ledger.load_health(args.ledger.parent / "health.json")
        waited = _since_last_run(health)
        if waited is not None and waited < interval:
            # Exit 0, not a dedicated code. From the workflow's point of view a skipped
            # attempt is a SUCCESS - the schedule is doing exactly what it was asked to -
            # and anything non-zero here would turn two of every three runs red and bury
            # the red run that means something.
            mins = int(waited.total_seconds() // 60)
            print(f"skipping: run #{health.last_run_id} finished {mins}m ago, "
                  f"less than --min-interval {args.min_interval}")
            return EXIT_OK
    if args.ledger is not None:
        result = runner.run_scheduled(
            ledger_path=args.ledger,
            health_path=args.ledger.parent / "health.json",
            dry_run=args.dry_run,
            seed=args.seed,
            profile_list=chosen,
        )
    else:
        with _db(args) as conn:
            result = runner.run(conn, dry_run=args.dry_run, seed=args.seed,
                                adapter=args.adapter, profile_list=chosen)
    for health in result.health:
        state = "ERR" if not health.ok else ("WARN" if health.error else "ok ")
        line = (f"  [{state}] {health.adapter}: {health.postings_seen} seen, "
                f"{health.duration_ms}ms")
        if health.error:
            line += f" - {health.error}"
        if health.ok and health.postings_seen == 0:
            line += "  <-- ZERO RESULTS, no error: check this"
        print(line)
    print(result.summary())
    for outcome in result.profiles.values():
        print("  " + outcome.summary())
    if result.seeded:
        print(f"seeded {result.seeded} postings as already-alerted; nothing was sent")
    if result.retried:
        print(f"NOTE: {result.retried} postings were re-delivered after an earlier run "
              f"failed to deliver or commit them")
    for canary in result.canaries:
        print(f"CANARY: {canary}", file=sys.stderr)
    # Build step 6.4: a pipeline problem is PUSHED, under a title that cannot be mistaken
    # for a job worth reading. It goes out on the same channel the postings do, because
    # that is the channel you actually read.
    #
    # An adapter that raised is reported here too. It used to only set the exit code, so
    # the only notice of a dead adapter was a red Actions run - i.e. an email that says
    # "workflow failed" and nothing else, which is exactly the notification this project
    # is supposed to replace.
    problems: list[str] = []
    if result.canaries:
        detail = "\n".join(str(c) for c in result.canaries)
        problems.append(f"zero-result regression:\n{detail}")
    # Driven by `error`, not by `ok`. An adapter that returned postings but could not
    # parse most of them is recorded ok-with-an-error, and it is exactly the failure the
    # zero-result canary cannot see.
    unhealthy = [h for h in result.health if h.error]
    if unhealthy:
        detail = "\n".join(f"{h.adapter}: {h.error}" for h in unhealthy)
        problems.append(f"adapter problem:\n{detail}")
    if not problems:
        return EXIT_OK
    if not args.dry_run and not args.seed:
        with ats_public.make_client() as client:
            notify.send_pipeline_alert(client, "\n\n".join(problems))
    # Non-zero so it is visible in the log, but a code `monitor.yml` knows means
    # "delivered, do not turn the run red". See the constants at the top of this file.
    return EXIT_PIPELINE_REPORTED


def cmd_list(args: argparse.Namespace) -> int:
    """One posting per line - company, title, location, eligibility, variant, grad window.

    With `--profile`, every line is scored and routed against that profile and the feed is
    ordered by its `sort_by` mode. `--resort` is the same thing said out loud: it re-ranks
    what is already stored, so you can test three orderings in a minute against real
    postings instead of waiting three hours for the next run (Phase X).

    `--resort` MAKES ZERO NETWORK CALLS. That constraint is the whole reason the feature is
    safe to experiment with, and TX.8 asserts it rather than assuming it.
    """
    since = parse_since(args.since)
    profile = None
    if args.profile or args.resort:
        try:
            profile = profiles.select(only=args.profile)[0] if args.profile \
                else profiles.select()[0]
        except profiles.ProfileError as exc:
            print(f"profile config error: {exc}", file=sys.stderr)
            return EXIT_MISCONFIGURED

    shown = 0
    with _db(args) as conn:
        rows = [
            (posting, analysis)
            for posting, analysis in store.iter_postings(
                conn, since=since,
                include_blocked=args.show_blocked or args.show_all,
                company=args.company,
            )
            if not (args.function and (not analysis or args.function not in analysis.function))
        ]
        if profile is None:
            for posting, analysis in rows:
                shown += 1
                print(_one_line(posting, analysis))
        else:
            from . import config_loader

            tier = config_loader.tier_companies(config_loader.load())
            applied = store.applied_ids(conn)
            candidates = [
                scoring.route(posting, analysis, profile, tier, applied)
                for posting, analysis in rows
                if analysis is not None
            ]
            if profile.screening.enabled:
                # CACHED verdicts only - `ensure` is never called, so this stays a
                # zero-network command. A dropped posting shows the quoted evidence.
                from .analyze import screen

                cached = screen.Screener(screen.load_cache(ledger.STATE_DIR))
                sink = runner.ProfileResult(profile=profile.name)
                candidates = [
                    c if c.route == "silent"
                    else runner._apply_screen(c, profile, cached, tier, applied, sink)
                    for c in candidates
                ]
            if not args.show_all:
                candidates = [c for c in candidates if c.route != "silent"]
            group = None
            for candidate in scoring.sort_candidates(candidates, profile):
                if profile.roles.priorities.sort_by == "priority" and candidate.group != group:
                    group = candidate.group
                    print(f"== {group} ==")
                shown += 1
                print(_candidate_line(candidate))
    if not shown:
        print(f"no postings since {args.since}")
    return 0


def _candidate_line(candidate) -> str:
    posting, analysis = candidate.posting, candidate.analysis
    where = ", ".join(posting.locations) or "-"
    bits = [
        f"{candidate.score:.2f}",
        f"{candidate.route:<7}",
        posting.global_id,
        f"{posting.company} - {posting.title}",
        where,
        analysis.eligibility,
        analysis.role_class,
    ]
    if candidate.route == "silent" and candidate.reasons:
        bits.append("(" + "; ".join(candidate.reasons) + ")")
    return " | ".join(bits)


def _one_line(posting, analysis) -> str:
    where = ", ".join(posting.locations) or "-"
    bits = [posting.global_id, f"{posting.company} - {posting.title}", where]
    if analysis:
        bits.append(analysis.eligibility)
        bits.append(analysis.role_class)
        bits.append(analysis.recommended_variant)
        if analysis.grad_window:
            bits.append(f"grad: {analysis.grad_window[:40]}")
    return " | ".join(bits)


def cmd_show(args: argparse.Namespace) -> int:
    """Prints the eligibility evidence sentences verbatim and in full. This command exists
    so a `blocked` label can be overruled by eye, in two seconds (spec 6)."""
    with _db(args) as conn:
        posting = store.get_posting(conn, args.global_id)
        if posting is None:
            print(f"unknown global_id {args.global_id!r}", file=sys.stderr)
            return 1
        analysis = store.get_analysis(conn, args.global_id)
        print(f"{posting.company} - {posting.title}")
        print(f"  locations : {', '.join(posting.locations) or '-'}")
        print(f"  remote    : {posting.remote_type or 'not stated'}")
        print(f"  posted    : {posting.posted_at or 'unknown'}")
        print(f"  first seen: {posting.first_seen_at}")
        print(f"  apply     : {posting.apply_url}")
        if analysis is None:
            print("  (no analysis row yet - run `jobmon reanalyze`)")
            return 0
        print(f"  role      : {analysis.role_class} | function: {analysis.function or '-'}")
        print(f"  grad      : {analysis.grad_window or 'not stated'}")
        print(f"  degree    : {'GATED' if analysis.degree_gated else 'not gated'}"
              f" | yoe floor: {analysis.yoe_floor if analysis.yoe_floor is not None else '-'}")
        print(f"  eligible  : {analysis.eligibility}")
        for sentence in analysis.eligibility_evidence:
            print(f"      > {sentence}")
        print(f"  variant   : {analysis.recommended_variant} {analysis.variant_scores}")
        if analysis.gaps:
            print(f"  gaps      : {', '.join(analysis.gaps)}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """`jobmon apply <global_id> --variant mle`. Records date and variant.
    This marks that YOU applied. The system never applies on your behalf (spec 1)."""
    with _db(args) as conn:
        try:
            store.record_application(conn, args.global_id, args.variant, datetime.now(UTC),
                                     args.note)
        except store.ApplicationError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    with _db(args) as conn:
        try:
            store.record_event(conn, args.global_id, args.status, datetime.now(UTC), args.note)
        except store.ApplicationError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


def cmd_funnel(args: argparse.Namespace) -> int:
    """Applications sent, callbacks, conversion by variant and function - the number that
    decides whether the volume strategy is working."""
    with _db(args) as conn:
        report = store.funnel_report(conn)
    print(f"applications sent: {report['applications_sent']}")
    print(f"callbacks:         {report['callbacks']}")
    print(f"conversion rate:   {report['conversion_rate']}%")
    for label, breakdown in (("variant", report["by_variant"]), ("function", report["by_function"])):
        if not breakdown:
            continue
        print(f"\nby {label}:")
        for key, stats in breakdown.items():
            print(f"  {key:<12} applied={stats['applied']:<4} "
                  f"callbacks={stats['callbacks']:<4} rate={stats['conversion_rate']}%")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Highlights adapters returning zero results without error - the failure mode that
    looks identical to a quiet week."""
    with _db(args) as conn:
        rows = store.health_report(conn)
    if not rows:
        print("no runs recorded yet")
        return 0
    for h in rows:
        flag = ""
        if h.ok and h.postings_seen == 0:
            flag = "  <-- ZERO RESULTS, no error"
        state = "ok " if h.ok else "ERR"
        print(f"{h.run_at:%Y-%m-%d %H:%M} [{state}] {h.adapter:<12} "
              f"seen={h.postings_seen:<6} {h.duration_ms}ms{flag}"
              + (f"  {h.error}" if h.error else ""))
    return 0


def cmd_reanalyze(args: argparse.Namespace) -> int:
    with _db(args) as conn:
        count = runner.analyze_pending(conn, reanalyze_all=args.all)
    print(f"analyzed {count} postings")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """`jobmon audit --days 7` (addendum 1). Measure before you filter."""
    with _db(args) as conn:
        report = audit.collect(conn, days=args.days, profile=args.profile)
    print(audit.render(report))
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    """Show what actually loaded, including the rank-derived category weights. The weights
    are the one part of the config you never write by hand, so they are the one part worth
    printing back."""
    try:
        loaded = profiles.select(only=args.profile)
    except profiles.ProfileError as exc:
        print(f"profile config error: {exc}", file=sys.stderr)
        return EXIT_MISCONFIGURED
    for profile in loaded:
        print(f"{profile.name}  ({profile.path or 'built-in defaults'})")
        pri = profile.roles.priorities
        ranked = ", ".join(f"{c} {pri.resolved[c]:.2f}" for c in pri.order) or "-"
        print(f"  priorities   order: {ranked}")
        print(f"               never: {', '.join(pri.never) or '-'}   "
              f"sort_by: {pri.sort_by}")
        print(f"  eligibility  needs_sponsorship={profile.eligibility.needs_sponsorship} "
              f"blocked={profile.eligibility.on_blocked} "
              f"unknown={profile.eligibility.on_unknown} "
              f"likely_ok={profile.eligibility.on_likely_ok}")
        print(f"  roles        {', '.join(profile.roles.role_class)} | "
              f"unclear={profile.roles.on_unclear_role} | "
              f"degree_gated={profile.roles.degree_gated} | "
              f"max_yoe={profile.roles.max_yoe}")
        lim = profile.limits
        print(f"  limits       min_score={lim.min_score} instant/day="
              f"{lim.max_instant_per_day} digest={lim.max_digest_items} "
              f"company/day={lim.max_per_company_per_day}")
        recap = (f"last {lim.recap_days}d, up to {lim.max_recap_items}"
                 if lim.recap_days else "off")
        print(f"  locations    allow={', '.join(profile.locations.allow) or 'anywhere'} | "
              f"on_mismatch={profile.locations.on_mismatch}")
        print(f"  recap        still-open section: {recap}")
        print(f"  denylist     {len(profile.denylist.companies)} companies, "
              f"{len(profile.denylist.title_terms)} title terms, "
              f"{len(profile.denylist.postings)} muted postings")
        print(f"  routing      instant={profile.routing.instant} "
              f"digest={profile.routing.digest}")
    return 0


def cmd_mute(args: argparse.Namespace) -> int:
    """`jobmon mute <global_id> --reason wrong-function` (addendum 6).

    Appends to the profile's denylist and records the reason. The profile YAML itself is
    NOT edited: the entry goes to `state/feedback/<profile>.jsonl` and is merged into the
    denylist at load time, so nothing you hand-wrote is silently changed (T8.11).
    """
    with _db(args) as conn:
        posting = store.get_posting(conn, args.global_id)
        if posting is None:
            print(f"unknown global_id {args.global_id!r}", file=sys.stderr)
            return 1
        analysis = store.get_analysis(conn, args.global_id)
    function = (analysis.function[0] if analysis and analysis.function else None)
    feedback.record(args.profile, "mute", args.global_id, reason=args.reason,
                    function=function, company=posting.company)
    print(f"muted {args.global_id} for profile {args.profile!r}"
          + (f" (reason: {args.reason})" if args.reason else ""))
    print(f"  recorded in {feedback.path_for(args.profile)}; "
          f"your profile YAML was not modified")
    return 0


def cmd_mute_company(args: argparse.Namespace) -> int:
    feedback.record(args.profile, "mute_company", args.name, reason=args.reason,
                    company=args.name)
    print(f"muted company {args.name!r} for profile {args.profile!r}")
    return 0


def cmd_boost(args: argparse.Namespace) -> int:
    with _db(args) as conn:
        if store.get_posting(conn, args.global_id) is None:
            print(f"unknown global_id {args.global_id!r}", file=sys.stderr)
            return 1
    feedback.record(args.profile, "boost", args.global_id)
    print(f"boosted {args.global_id} for profile {args.profile!r}")
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    """Report the most common mute reason and propose a concrete profile edit.

    It PROPOSES. You edit the YAML. There is no automatic self-tuning here on purpose: a
    system that silently changes its own filters is one that silently stops showing you
    jobs.
    """
    summary, proposals = feedback.tune(args.profile)
    print(summary)
    for proposal in proposals:
        print()
        print(proposal)
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    with _db(args) as conn:
        store.init_db(conn)
    print(f"initialized {args.db}")
    return 0


def _db(args: argparse.Namespace):
    conn = store.connect(Path(args.db))
    store.init_db(conn)
    return conn


COMMANDS = {
    "init-db": cmd_init_db,
    "run": cmd_run,
    "list": cmd_list,
    "show": cmd_show,
    "apply": cmd_apply,
    "log": cmd_log,
    "funnel": cmd_funnel,
    "health": cmd_health,
    "reanalyze": cmd_reanalyze,
    "audit": cmd_audit,
    "profiles": cmd_profiles,
    "mute": cmd_mute,
    "mute-company": cmd_mute_company,
    "boost": cmd_boost,
    "tune": cmd_tune,
}


def main(argv: list[str] | None = None) -> int:
    """Entry point. A failed run exits non-zero so the Actions cron surfaces it."""
    # Posting titles now come straight from free-text READMEs (github_repos, build step
    # 7b) and routinely carry emoji ("Engineering Intern \U0001f393"). GitHub Actions'
    # ubuntu runner is UTF-8 by default, but a local Windows console (cp1252) is not, and
    # `print()` hitting one crashed the whole run - `--dry-run` on a real fetch, not a
    # synthetic test, is what surfaced this. Replace what the console can't render rather
    # than raise; reconfigure is a no-op on an already-UTF-8 stream.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return COMMANDS[args.command](args)
    except NotImplementedError as exc:
        print(f"not built yet: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
