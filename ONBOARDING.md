# jobmon — friend setup guide

jobmon watches job boards (Greenhouse, Lever, Ashby, Workable + curated new-grad/intern
repo lists) hourly, scores each posting against **your** interests, and emails/pushes a
digest. One person's copy can serve several people — you just each get your own profile
file. This doc is the short version; `README.md` has the full detail on every knob.

## 1. Get your own copy running

    git clone <this repo>
    cd app_tracker
    python -m venv .venv && .venv/Scripts/activate     # Windows; use `source .venv/bin/activate` on macOS/Linux
    pip install -e ".[dev]"
    jobmon init-db
    jobmon run --dry-run          # sanity check: prints what it *would* send, sends nothing
    jobmon run --seed             # ONCE on a fresh setup — marks everything currently open
                                   # as already-seen so your first real alert isn't 13,000 jobs

To get alerts on a schedule without leaving your laptop on, push your fork to GitHub and
let `.github/workflows/monitor.yml` run it hourly instead (see README "Running it on
GitHub Actions" for the two required secrets: an ntfy topic and/or SMTP email settings).

## 2. Set up your role / area of interest

**This is the only file you need to touch.** Everything about what a posting *is* (its
role, function, location, visa signal) is computed once and shared. What you personally
want is a separate YAML file — add yours at:

    jobmon/config/profiles/<your-name>.yaml

Copy `jobmon/config/profiles/jonathan.yaml` as a starting point, or start minimal — every
field has a default, so this alone is a valid profile:

```yaml
name: alex
roles:
  role_class: [new_grad, internship]   # also: experienced, unclear
```

The fields you'll actually want to change:

```yaml
name: alex
locale: us

eligibility:
  needs_sponsorship: false   # true if you need visa sponsorship; false skips that whole check

roles:
  role_class: [new_grad, internship]        # which career stage you're targeting
  priorities:
    order: [swe, mle, data, security, systems]  # your functions, best fit first
    never: [quant, sdet]                        # never show me these, no matter the score
  max_yoe: 2                                    # cap on required years of experience

locations:
  allow: [remote, "United States"]              # postings outside this list are hidden
  prefer: ["New York", "Bay Area"]              # ranked higher, not required

limits:
  min_score: 0.30            # lower = more emails. Tune with `jobmon audit --days 7`
  max_digest_items: 25       # hard cap per digest, regardless of score

routing:
  instant: all      # ntfy | email | all | none — top-scoring postings, pushed immediately
  digest: email      # every hourly sweep
```

`role_class` options: `new_grad`, `internship`, `experienced`, `unclear`.
`priorities` functions: `swe`, `mle`, `data`, `security`, `systems`, `research`, `quant`.

Run for just your profile:

    jobmon run --profile alex

Leave off `--profile` and jobmon runs every profile file in that folder — so one
scheduled GitHub Actions run can alert everyone at once, with zero extra fetching per
person added.

**A profile with a typo will refuse to run** rather than silently falling back to "alert
everything" — it'll name the bad field. That's intentional.

## 3. How it works (architecture)

    [board adapters] -> [normalize] -> [dedup store] -> [analyze: objective facts]
                                                              |
                                                +-------------+-------------+
                                                |                           |
                                        [your profile]              [friend's profile]
                                                |                           |
                                            [score + route]           [score + route]
                                                |                           |
                                          [alert: you]               [alert: friend]

- **Adapters** (`jobmon/adapters/`) pull raw postings from each source (public ATS APIs,
  curated GitHub tracker repos).
- **Normalize/dedup** (`normalize.py`, `store.py`) collapse the same req posted under
  multiple titles/sources into one record, and remember what's already been seen.
- **Analyze** (`filters/`, `analyze/`) computes *objective* facts about a posting once —
  role class, function, keyword matches, degree/YoE requirements, visa/sponsorship
  signal — shared by every profile.
- **Profiles** (`jobmon/config/profiles/*.yaml`) turn those facts into a score and a
  routing decision (instant / digest / silent) using *your* priorities, caps, and
  denylist. This is the only subjective layer, and the only thing you need to edit.
- **Notify** (`notify.py`) sends the result via ntfy (push) and/or email, one digest per
  sweep, with a "still open" recap so a quiet hour isn't an empty email.
- **State** (`state/`) is a set of committed JSONL files — one global postings log, one
  alerted-log and one feedback-log per profile. That's the entire database; there's no
  server, no queue, no container.

## 4. Everyday commands

| Command | Purpose |
|---|---|
| `jobmon run --profile <name>` | One fetch → score → alert pass, for one person |
| `jobmon list --since 24h --profile <name>` | See what's stored without re-fetching |
| `jobmon mute <id> --profile <name> --reason ...` | Never show me this posting again |
| `jobmon apply <id> --variant <resume>` | Mark applied, tracked in `jobmon funnel` |
| `jobmon audit --days 7` | What was actually sent, and which knob would change it |

If your inbox has too much or too little, run `jobmon audit --days 7` first — it tells
you which specific setting to change instead of guessing. See `README.md` for the full
walkthrough on scoring, sponsorship confidence, and tuning caps.
