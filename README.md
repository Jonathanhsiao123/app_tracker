# jobmon

Job posting monitor for the 2027 New Grad / Summer 2027 Internship cycle (US market).
See `job-monitor-spec.md` for the authoritative spec.

**Status (2026-09-12): Phase 1 and 2 ship; Phase 3 is mostly done.** Fetch, normalize,
dedup, role filter, ntfy alerts, eligibility classification, keyword/variant analysis,
apply/log/funnel/health tracking, and the curated-repo adapter (`adapters/github_repos.py`,
covering SimplifyJobs/vanshb03/speedyapply) are all implemented and tested. Only
`adapters/bigtech/` is still a stub — build only if time remains under the day-7 timebox
(spec 12); the curated repos are the primary big-tech source regardless (spec 5b).

    [source adapters] -> [normalizer] -> [dedup store] -> [filters] -> [analyzer] -> [notifier]

Single Python process, run on a schedule (GitHub Actions cron, every 3 hours). No services,
no queues, no containers. The system never applies on your behalf.

## Quick start

    python -m venv .venv && .venv/Scripts/activate     # Windows
    pip install -e ".[dev]"
    jobmon init-db
    jobmon run --dry-run          # print what would be sent, send nothing
    jobmon run --seed             # ONCE, on a fresh DB: see the warning below
    JOBMON_NTFY_TOPIC=... jobmon run

**Seed the database before the first real run.** A fresh database has seen nothing, so the
first pass treats every currently-open posting as new - 13,315 of them on the configured
board set. `jobmon run --seed` stores and marks them all as already-alerted without sending.
Skip it and the first cron run pushes your entire backlog to your phone.

## Running it on GitHub Actions

`.github/workflows/monitor.yml` runs one pass every 3 hours. Two things have to be true or
it cannot notify you:

1. **Set the `JOBMON_NTFY_TOPIC` repository secret.** Settings > Secrets and variables >
   Actions > New repository secret, or `gh secret set JOBMON_NTFY_TOPIC`. The topic is any
   hard-to-guess string; subscribe to the same string in the ntfy app. Without it `jobmon
   run` refuses up front and exits 2, and the Actions run goes red - that red run is the
   only channel left to tell you the secret is missing.
2. **Let `state/seen.jsonl` get committed.** It is the only durable state. The workflow
   writes it, commits it and pushes it back to the branch; the SQLite file is a derived
   cache rebuilt from it each run and thrown away. If the ledger is missing from the
   checkout, the workflow seeds instead of alerting: it records every currently-open
   posting as already-seen and sends nothing, so you get one quiet cycle rather than
   13,000 notifications. You do not need to run `--seed` by hand for the cron path.

**A red Actions run means jobmon could not reach you, and nothing else.** A dead adapter or
a board that dropped to zero is pushed to your ntfy topic under the title
`!! jobmon PIPELINE`, and the run exits 3, which the workflow treats as green. Only a crash
or a missing topic turns the run red - otherwise the emails you get from GitHub would be
competing with the alerts this project exists to send.

| Exit code | Meaning |
|---|---|
| 0 | clean pass |
| 2 | misconfigured, nothing ran (usually `JOBMON_NTFY_TOPIC` unset) |
| 3 | ran; a pipeline problem was pushed to the ntfy topic |

## How much reaches your phone

Tier-1 companies alert individually, everything else is batched into one digest - but the
individual alerts are capped at `notify.MAX_INSTANT_ALERTS` (20) per run. Spec 10 hand-lists
about 20 tier-1 *companies*, which is not 20 *postings*: a cold sweep of the configured
boards finds 697 open tier-1 roles, and 697 separate pushes is not an instant alert. It also
exceeds ntfy.sh's rate limit (60 requests, then one per 5s), so most of them would be
refused with a 429 and never arrive. Anything over the cap joins the digest, so nothing is
dropped - it is batched.

In steady state, with the ledger committed, a run alerts on the handful of postings that
appeared in the last 3 hours and the cap never binds.

## Commands

| Command | Purpose |
|---|---|
| `jobmon run` | One pass: fetch -> normalize -> dedup -> filter -> analyze -> notify |
| `jobmon list --since 24h [--show-blocked]` | Print stored postings |
| `jobmon show <global_id>` | Full record + analysis + eligibility evidence |
| `jobmon apply <global_id> --variant mle` | Mark applied, record date and variant |
| `jobmon log <global_id> --status screen\|reject\|onsite\|offer` | Advance funnel stage |
| `jobmon funnel` | Applications, callbacks, conversion by variant and function |
| `jobmon health` | Per-adapter last-run counts and failures |

## Build order (spec 12)

- **Phase 1 (day 2): DONE.** `adapters/ats_public.py`, `normalize.py`, `store.py`,
  `filters/role.py`, `notify.py`, `runner.py`, `cli.py` — ntfy alerts working end to end.
  Scoped to four ATSes (greenhouse, lever, ashby, workable); smartrecruiters is deferred
  because it is the only one needing pagination plus an N+1 detail fetch.
- **Phase 2 (day 4): DONE.** `filters/eligibility.py`, `taxonomy.yaml`, `analyze/keywords.py`,
  `analyze/variants.py`. `sponsorship_overrides.yaml` is wired and beats text classification,
  but is still empty (`{}`) — it needs the one-evening hand-seeding pass from DOL LCA/PERM
  disclosure data described in spec 6.
- **Phase 3 (day 7): 7a and 7b done.** `apply`/`log`/`funnel`/`health` tracking (7a) and
  `adapters/github_repos.py` (7b, SimplifyJobs + vanshb03 + speedyapply) both ship.
  `adapters/bigtech/` (7c) is still a stub — build only if time remains; 7b already covers
  big tech better than a handful of hand-reverse-engineered portals would (spec 5b).
- **Phase 4:** deferred. Taiwan / China. Design notes only, do not build.

## Scope discipline

Ship the ugly version. No dashboard, no LLM rewriting, no abstractions for adapters not yet
written. If Phase 3 is not done by day 7, stop and use what exists.
