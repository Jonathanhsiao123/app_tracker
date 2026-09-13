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

from . import notify, runner, store
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

    list_cmd = sub.add_parser("list", help="stored postings, newest first")
    list_cmd.add_argument("--since", default="24h", help="e.g. 24h, 3d, 2w")
    list_cmd.add_argument("--show-blocked", action="store_true",
                          help="include postings suppressed as ineligible")
    list_cmd.add_argument("--company")
    list_cmd.add_argument("--function", help="filter to one function, e.g. mle")

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
    if args.ledger is not None:
        result = runner.run_scheduled(
            ledger_path=args.ledger,
            health_path=args.ledger.parent / "health.json",
            dry_run=args.dry_run,
            seed=args.seed,
        )
    else:
        with _db(args) as conn:
            result = runner.run(conn, dry_run=args.dry_run, seed=args.seed,
                                adapter=args.adapter)
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
    """One posting per line - company, title, location, eligibility, variant, grad window."""
    since = parse_since(args.since)
    shown = 0
    with _db(args) as conn:
        for posting, analysis in store.iter_postings(
            conn, since=since, include_blocked=args.show_blocked, company=args.company
        ):
            if args.function and (not analysis or args.function not in analysis.function):
                continue
            shown += 1
            print(_one_line(posting, analysis))
    if not shown:
        print(f"no postings since {args.since}")
    return 0


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
        store.record_application(conn, args.global_id, args.variant, datetime.now(UTC),
                                 args.note)
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    with _db(args) as conn:
        store.record_event(conn, args.global_id, args.status, datetime.now(UTC), args.note)
    return 0


def cmd_funnel(args: argparse.Namespace) -> int:
    """Applications sent, callbacks, conversion by variant and function - the number that
    decides whether the volume strategy is working."""
    with _db(args) as conn:
        report = store.funnel_report(conn)
    for key, value in report.items():
        print(f"{key}: {value}")
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
