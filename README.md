# jobmon

Job posting monitor for the 2027 New Grad / Summer 2027 Internship cycle (US market).
See `job-monitor-spec.md` for the authoritative spec and
`profiles-and-eligibility-addendum.md` for the profiles, scoring and alert-policy layer.

**Status (2026-09-17): Phases 1–3 ship, and the addendum ships.** Fetch, normalize, dedup,
role filter, eligibility classification, keyword/variant analysis, apply/log/funnel/health
tracking, and the curated-repo adapter (`adapters/github_repos.py`, covering
SimplifyJobs/vanshb03/speedyapply) are all implemented and tested. So are the addendum's
seven build steps: the per-profile persistence split, `jobmon audit`, profiles, alert
policy and caps, scoring and routing, sponsorship confidence, and `mute`/`tune` — plus
Phase X category priorities. Only `adapters/bigtech/` is still a stub (spec 12); the
curated repos are the primary big-tech source regardless (spec 5b).

**2026-09-17 — alert volume repair.** The tuning had overshot: a measured sweep showed
`min_score: 0.55` admitting **0 of 321** daily candidates, so the hourly mail was empty and
the few postings that did qualify went to the phone only. Four fixes, each measured before
and after — see `tests/test_recap_and_cadence.py`:

| Defect | Before | After |
|---|---|---|
| `unknown` sponsor confidence penalized twice | 321/321 postings docked 0.175 | `unknown` exempt; penalty only on observed-low |
| digest listed only what was new this hour | ~1–2 items, often 0 | plus a **still-open** section from the last 3 days |
| `locations.allow` was a scoring hint, not a filter | 57% of items outside the US | suppressed by default (`on_mismatch`) |
| same req from 3 sources under 3 titles | 14% redundant copies | exact ATS-req-id dedup, 0% |

The hourly schedule was also only landing a run every ~5 hours — GitHub drops `schedule`
events under load — so the workflow now makes three staggered attempts an hour and
`--min-interval` keeps that to one digest.

    [source adapters] -> [normalizer] -> [dedup store] -> ANALYZE (objective) -> [profile A] -> alerts  (new + still-open)
                                                                              -> [profile B] -> alerts

Single Python process, run on an hourly schedule (GitHub Actions cron). No services,
no queues, no containers. The system never applies on your behalf.

**Analysis is objective; profiles are subjective.** The eligibility label, evidence
sentence, keyword matches, degree flag, YoE floor, role class, function and locations are
facts about a posting: computed once, stored once, shared by every profile. What to
suppress, what to instant-alert, what counts as a good fit and how many alerts a day are
opinions, and they live in `jobmon/config/profiles/<name>.yaml`. Consequently a second user
costs zero extra fetching and zero extra analysis, and changing your own preferences never
requires a re-crawl.

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

`.github/workflows/monitor.yml` runs one pass every hour. Two things have to be true or
it cannot notify you:

1. **Set the `JOBMON_NTFY_TOPIC` repository secret.** Settings > Secrets and variables >
   Actions > New repository secret, or `gh secret set JOBMON_NTFY_TOPIC`. The topic is any
   hard-to-guess string; subscribe to the same string in the ntfy app. Without it `jobmon
   run` refuses up front and exits 2, and the Actions run goes red - that red run is the
   only channel left to tell you the secret is missing.
2. **Let `state/` get committed.** It is the only durable state. The workflow writes it,
   commits it and pushes it back to the branch; the SQLite file is a derived cache rebuilt
   from it each run and thrown away. If the ledger is missing from the checkout, the
   workflow seeds instead of alerting: it records every currently-open posting as
   already-seen and sends nothing, so you get one quiet cycle rather than 13,000
   notifications. You do not need to run `--seed` by hand for the cron path.

   | File | Holds |
   |---|---|
   | `state/postings.jsonl` | one line per posting — no alert state |
   | `state/alerted/<profile>.jsonl` | one line per alert delivered to that profile |
   | `state/feedback/<profile>.jsonl` | what you muted and boosted, and why |
   | `state/health.json` | per-adapter counts from the last run, for the canary |

   A pre-addendum `state/seen.jsonl` is migrated into the first two on the next run and
   then left alone — nothing is deleted, and `--ledger state/seen.jsonl` still resolves.

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

### Email delivery

Every sweep delivers one digest by SMTP. Push delivery remains enabled; when SMTP is
configured, both channels must succeed before a posting is marked alerted. Set these
GitHub Actions secrets to enable it:

`JOBMON_SMTP_HOST`, `JOBMON_SMTP_PORT` (usually `587`, or `465` for implicit TLS),
`JOBMON_SMTP_USER`, `JOBMON_SMTP_PASSWORD`, `JOBMON_EMAIL_FROM`, and `JOBMON_EMAIL_TO`.

Leave all six unset for ntfy-only operation. The SMTP password is read only from the
Actions secret and is never written to the ledger or logs. For local runs, fill in `.env`
(gitignored; copy it from `.env.example`, which lists every setting). `jobmon` reads it on
start, and anything already set in the shell takes precedence.

### Clearance screening (LLM, optional)

With `screening.enabled: true` in a profile, a model hosted on Luminal Cloud reads every
posting before it is sent and drops the ones that require a security clearance or US
citizenship / US-person status. The regex filter misses these when the requirement is
phrased loosely or sits in the last paragraph. It reads the whole description, or for
curated-list rows (which have no body) the apply page itself, and both the NEW and STILL
OPEN sections are screened. Calls go to an OpenAI-style `/chat/completions` endpoint.

* **Turn on:** set the `LUMINAL_API_KEY` and `LUMINAL_BASE_URL` repository secrets (the base
  URL is everything before `/chat/completions`), put your deployment's model name in
  `screening.model` (or the repository variable `LUMINAL_MODEL`), and set
  `screening.enabled: true` (already on in `jonathan.yaml`).
* **Turn off:** `screening.enabled: false`, or for no-commit control set the repository
  *variable* `JOBMON_LLM_SCREEN=off`.
* **Fails open:** any of those unset, a wrong key or model name, an outage, or a spent
  `max_calls_per_run` budget delivers postings under the regex rules alone and says so in
  the digest footer. The run is never failed.
* **Cost:** each verdict is cached in `state/screened.jsonl`, so a posting is paid for once:
  roughly 30-150 calls a day of ~3k tokens each, drawn from the Luminal account's credit.
* **Auditing:** a dropped posting shows its quoted evidence in `jobmon list --all`.

When a posting matches no category from its title, the screen's category is used to file it.

## How much reaches your phone

Each sweep sends one digest, subject `Jobmon Opening Workflow`, in two sections:

**NEW THIS SWEEP** — postings found this run that you have never been shown, in full
detail: visa badge, evidence sentence, keywords, gaps, recommended resume variant.

**STILL OPEN** — the highest-scoring postings from the last `limits.recap_days` that are
still live and that you have not applied to, one compact line each, grouped by category in
your `priorities.order`.

The second section exists because an hourly sweep finds roughly **one or two** genuinely
new qualifying postings — about 40 a day. A digest carrying only those is mostly empty, and
on a quiet hour it used to be a single sentence saying so. Recap items are **never marked
alerted**: they recur every sweep until the employer takes the posting down or you run
`jobmon apply`, which is what makes it a standing list rather than a second chance to miss
something. Set `recap_days: 0` to turn the section off.

A digest with nothing in either section still sends, so the mail is itself the proof that
the sweep ran — there is no separate heartbeat. When the schedule has slipped, the footer
says by how much.

### Why three cron entries

The workflow fires at `:07`, `:27` and `:47`. GitHub delivers `schedule` events on a
best-effort basis and **drops** them under load: measured on this repo, a nominally hourly
cron produced run ids 23 through 33 over 37 hours — ten sweeps where there should have been
thirty-seven, with contiguous ids and no red runs, so nothing had crashed. One cron entry
gives that failure one chance to lose the hour; three give it three.

`jobmon run --min-interval 50m` is what stops three attempts becoming three digests: an
attempt that finds a completed run less than 50 minutes old prints one line and exits 0
without sweeping. It is passed only for `schedule`, so clicking **Run workflow** always
runs.

## Commands

| Command | Purpose |
|---|---|
| `jobmon run [--profile <name>]` | One pass: fetch -> normalize -> dedup -> analyze -> route -> notify |
| `jobmon audit --days 7` | **What was actually sent, and which lever would cut it** |
| `jobmon list --since 24h [--profile <name>] [--all] [--resort]` | Print stored postings |
| `jobmon profiles` | Loaded profiles and their resolved category weights |
| `jobmon show <global_id>` | Full record + analysis + eligibility evidence |
| `jobmon mute <global_id> --profile <name> --reason wrong-function` | Never show me this again |
| `jobmon mute-company <name> --profile <name>` | Never show me this employer again |
| `jobmon boost <global_id> --profile <name>` | Rank this posting higher |
| `jobmon tune --profile <name>` | Propose a profile edit from your mutes — proposes only |
| `jobmon apply <global_id> --variant mle` | Mark applied, record date and variant |
| `jobmon log <global_id> --status screen\|reject\|onsite\|offer` | Advance funnel stage |
| `jobmon funnel` | Applications, callbacks, conversion by variant and function |
| `jobmon health` | Per-adapter last-run counts and failures |

## Too many alerts? Measure before you filter

The instinct is to add filters. Do the measurement first — the fix is usually a cap and a
pruned company list, not new code.

    jobmon audit --days 7

It prints what was sent, broken down by source, company, function, role class and
eligibility, plus the alert-to-apply rate, and then tells you which of the three levers
matters for *your* distribution:

- **`unclear` dominating `role_class`** — `filters/role.py` is passing everything it cannot
  classify. Set `roles.on_unclear_role: digest`.
- **`unknown` dominating `eligibility`** — the majority case, alerting at full priority.
  Set `eligibility.on_unknown: digest`. The single largest noise cut available.
- **A handful of companies producing most alerts** — `companies.yaml` picked up large
  employers. Prune them, or set `limits.max_per_company_per_day`.
- **Alert-to-apply under 5%** — the threshold is wrong, not the sources. Raise
  `limits.min_score`.

Neither of the first two deletes anything. Both are still retrievable with
`jobmon list --all`.

## Profiles

One YAML file per user in `jobmon/config/profiles/`. Every field has a default in code, so
a minimal profile is four lines:

```yaml
name: jonathan
roles:
  role_class: [new_grad, internship]
```

`jobmon run --profile jonathan` delivers for one; no flag delivers for all of them. See
`jobmon/config/profiles/jonathan.yaml` for the full annotated schema — work authorization,
role relevance, locations, noise caps, denylists and routing.

**A profile that will not parse stops the run.** An unknown field, a misspelled enum value,
or a category listed in both `order` and `never` all fail at load with the offending field
named. There is deliberately no fall-back-to-permissive path: a config typo must never
restore the flood you edited the file to stop.

### Category priorities (Phase X)

The part of the config you keep editing. An ordered list, not a dict of floats — tuning six
numbers to produce a ranking you already know in your head is fiddly and fails silently:

```yaml
roles:
  priorities:
    order: [mle, security, systems, swe, data]   # highest first. Move lines to re-rank.
    never: [quant, sdet]                         # stored, never alerted, at any score
    sort_by: priority                            # priority | score | date | company
    weights: {data: 0.15}                        # escape hatch, for spacing only
```

Edit the YAML and save; the next run picks it up. To see the effect on postings you already
have, with no fetch and no re-analysis:

    jobmon list --profile jonathan --resort

`sort_by: priority` groups before it scores, so a strong `swe` match sits below mediocre
`mle` ones. That is intended, and it is also the mode's weakness: if you find yourself
routinely scrolling past the first group, switch to `score` and let `order` act as a
tiebreaker. The scrolling is the signal — do not try to fix it by re-ranking.

### locations.allow is an allow-list

`allow` suppresses postings matching neither `allow` nor `prefer`, which is what the word
means. It used to feed the score term and nothing else, so a profile reading
`allow: [remote, "United States"]` was quietly offered jobs in Sydney, Amsterdam and
Tampere — 57% of alertable postings on the measured sweep. Set
`locations.on_mismatch: digest` (or `alert`) to get the old lenient behaviour back under a
name that admits to it. An empty `allow` no-ops, so a profile that has not thought about
location is unaffected.

## Scoring and routing

    score = w_function    * function_match      (rank-derived, from priorities.order)
          + w_keywords    * variant_top_score
          + w_location    * location_match      (allow = 0.5, prefer = 1.0)
          + w_eligibility * sponsor_confidence  (frequent = 1.0 ... none_observed = 0.3)
          + w_tier        * company_tier
          - penalty_degree_gated
          - penalty_yoe_over

Weights live in the profile and default to summing to 1.0. Routing is the **quieter** of a
policy ceiling and a score proposal: score ≥ 0.8 or a tier-1 company proposes an instant
alert, score ≥ `min_score` proposes the digest, and anything the eligibility/role policy
caps at `digest` stays in the digest however well it scores. Then the caps in `limits`
apply, highest score first.

`unknown` sponsor confidence is **not** charged `penalty_below_min_sponsor`. It already
scores 0.5, mid-scale, and no DOL match means a fuzzy-name miss or a small employer rather
than evidence of refusal — charging both scored the same missing fact twice. That mattered
more than it sounds: with `sponsor_history.yaml` unpopulated every employer is `unknown`,
so the penalty applied to **321 of 321** candidates on a measured sweep. A penalty that
fires on everything ranks nothing; it is just `min_score + 0.10` with the signal removed.
Together with the halved eligibility term it docked 0.175 from every posting, and
`min_score: 0.55` admitted **none of them**.

**Set `min_score` from `jobmon audit`, never from intuition.** The measured yield for the
shipped profile was 13 items/day at 0.55, 33 at 0.40 and 43 at 0.30. The caps, not the
threshold, are what bound your inbox — so a loose threshold with tight caps hands you the
best 25, while a tight one hands you an empty inbox and no ranking at all.

The caps are the hard guarantee: **even if every threshold is miscalibrated, you cannot
receive more than `max_instant_per_day` plus two digests.** Everything they cut is still
stored, and the digest says how much was held back.

## Sponsorship confidence

Posting text is a weak signal for visa support — companies that sponsor routinely usually
say nothing, and boilerplate refusal language sometimes sits on reqs a recruiter will
override. Company filing history is stronger, and public.

    python scripts/build_sponsor_history.py --input <dir of DOL LCA CSVs>

builds `jobmon/config/sponsor_history.yaml` from the DOL disclosure files (public,
quarterly, bulk download, no scraping).

That file currently holds a **hand-seeded 17 employers**, transcribed from the per-company
research already in `sponsorship_overrides.yaml` with their filing counts and dates intact.
It is a seed, not the real thing — 17 of 172 configured companies — and running the builder
will overwrite it, which is the intent. Everything unlisted stays `unknown`, which is the
honest label: no DOL match means a fuzzy-name miss or a small employer, never a refusal.

It **combines** with the text classifier and never replaces it:

| Text label | History | Result |
|---|---|---|
| `blocked` | any | `blocked` — explicit refusal always wins |
| `likely_ok` | any | `likely_ok` |
| `unknown` | `frequent` | `likely_ok`, evidence names the filing count |
| `unknown` | `occasional` | `unknown`, confidence noted |
| `unknown` | `none_observed` | `unknown`, flagged low confidence |

Every alert carries a one-line badge, and the limitations are stated in it rather than
hidden:

    VISA: likely_ok - no posting language; employer filed 240 H-1B petitions since 2024
    VISA: blocked   - "Must be a U.S. citizen."
    VISA: unknown   - employer has no observed H-1B history

LCA filings are H-1B petitions, not a promise about this req. Name matching is fuzzy and
subsidiaries fragment. `none_observed` is weak evidence, not a block. Clearance and ITAR are
the only true hard blocks in this system — everything else is a prior, so
`min_sponsor_confidence` downranks rather than suppresses. A profile with
`needs_sponsorship: false` omits the badge entirely.

## Feedback

    jobmon mute <global_id> --profile jonathan --reason wrong-function
    jobmon mute-company "Flood Corp" --profile jonathan
    jobmon boost <global_id> --profile jonathan
    jobmon tune --profile jonathan

Mutes append to `state/feedback/<profile>.jsonl` and are merged into the profile's denylist
at load time; your hand-written YAML is never rewritten. `tune` reports your most common
mute reason and proposes a concrete edit — **it proposes, you edit**. There is no automatic
self-tuning: a system that silently changes its own filters is one that silently stops
showing you jobs.

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

### Addendum build order (all done)

1. Split `seen.jsonl` into `postings.jsonl` and `alerted/<profile>.jsonl`. Painful to
   retrofit later, so first.
2. `jobmon audit --days 7`. Measure before filtering.
3. Profile schema plus loader, with the pre-addendum behaviour as the built-in default so
   nothing changes on day one.
4. `on_unknown: digest`, `on_unclear_role: digest`, and the `limits` caps.
5. Scoring and routing. (**Phase X** slots in between 3 and 5, since scoring reads the rank
   it produces.)
6. `sponsor_history.yaml` builder plus the badge.
7. `mute` and `tune`.

## Scope discipline

Ship the ugly version. No dashboard, no LLM rewriting, no abstractions for adapters not yet
written. If Phase 3 is not done by day 7, stop and use what exists.
