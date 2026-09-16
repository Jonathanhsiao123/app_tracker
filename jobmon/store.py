"""SQLite storage and dedup (spec 4). PHASE 1; application tracking tables are PHASE 3.

One file, committed nowhere. No ORM, no migrations framework - `SCHEMA` is executed with
`CREATE TABLE IF NOT EXISTS` and altered by hand if it ever needs to change.

Nothing is ever deleted. `blocked` postings are stored and shown by `--show-blocked`;
closed and duplicate postings are stored so churn is visible.

Repost detection needs run identity: "disappeared then came back" is only observable
relative to which run last saw a posting. Hence the `runs` table and `last_seen_run` - a
posting whose previous sighting was more than one run ago has been reposted.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import AdapterHealth, Analysis, ApplicationStatus, Posting
from .normalize import normalize_company, normalize_title

DB_PATH = Path("jobmon.db")

FUZZY_WINDOW_DAYS = 60

# The profile every per-profile write is attributed to when the caller names none. Keeps
# every single-profile call site (and every pre-addendum test) working unchanged.
DEFAULT_PROFILE = "default"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS postings (
    global_id           TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    company             TEXT NOT NULL,
    normalized_company  TEXT NOT NULL,
    title               TEXT NOT NULL,
    normalized_title    TEXT NOT NULL,
    first_location      TEXT,
    locations_json      TEXT NOT NULL,
    remote_type         TEXT,
    description_html    TEXT NOT NULL,
    description_text    TEXT NOT NULL,
    apply_url           TEXT NOT NULL,
    posted_at           TEXT,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    raw_json            TEXT NOT NULL,
    locale              TEXT NOT NULL DEFAULT 'US',
    reposted_count      INTEGER NOT NULL DEFAULT 0,
    last_seen_run       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_postings_fuzzy
    ON postings (normalized_company, normalized_title, first_location);
CREATE INDEX IF NOT EXISTS idx_postings_first_seen ON postings (first_seen_at);

-- Addendum 2, build step 1. "We have this posting" and "we alerted about it" are two
-- different facts, and they used to be one column (`postings.alerted_at`). With profiles
-- they cannot be: profile A alerting on a posting must not silence profile B, which is
-- exactly what a single global flag does. One row per (posting, profile).
CREATE TABLE IF NOT EXISTS alerts (
    global_id  TEXT NOT NULL REFERENCES postings(global_id),
    profile    TEXT NOT NULL,
    alerted_at TEXT NOT NULL,
    channel    TEXT NOT NULL,
    score      REAL,
    PRIMARY KEY (global_id, profile)
);
CREATE INDEX IF NOT EXISTS idx_alerts_profile_time ON alerts (profile, alerted_at);

CREATE TABLE IF NOT EXISTS duplicates (
    global_id         TEXT NOT NULL,
    matched_global_id TEXT NOT NULL,
    matched_on        TEXT NOT NULL,
    seen_at           TEXT NOT NULL,
    PRIMARY KEY (global_id, matched_global_id)
);

CREATE TABLE IF NOT EXISTS analysis (
    global_id                 TEXT PRIMARY KEY REFERENCES postings(global_id),
    role_class                TEXT NOT NULL,
    function_json             TEXT NOT NULL,
    eligibility               TEXT NOT NULL,
    eligibility_evidence_json TEXT NOT NULL,
    grad_window               TEXT,
    keywords_json             TEXT NOT NULL,
    gaps_json                 TEXT NOT NULL,
    recommended_variant       TEXT NOT NULL,
    variant_scores_json       TEXT NOT NULL,
    degree_gated              INTEGER NOT NULL DEFAULT 0,
    yoe_floor                 INTEGER,
    text_eligibility          TEXT NOT NULL DEFAULT 'unknown',
    sponsor_confidence        TEXT NOT NULL DEFAULT 'unknown',
    sponsor_evidence          TEXT,
    blocking_groups_json      TEXT NOT NULL DEFAULT '[]',
    analyzed_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    global_id  TEXT PRIMARY KEY REFERENCES postings(global_id),
    variant    TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS application_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    global_id   TEXT NOT NULL REFERENCES postings(global_id),
    status      TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS adapter_health (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    adapter       TEXT NOT NULL,
    run_at        TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    postings_seen INTEGER NOT NULL,
    new_postings  INTEGER NOT NULL,
    duration_ms   INTEGER NOT NULL,
    error         TEXT
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open the database, enabling foreign keys and WAL."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


# Columns added after `SCHEMA` first shipped. `CREATE TABLE IF NOT EXISTS` is a no-op on
# an existing table, so a jobmon.db from before the addendum would keep its old `analysis`
# shape and every insert would fail on the new column list. Still no migrations framework -
# just the list of what to add, applied when absent.
ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("analysis", "text_eligibility", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("analysis", "sponsor_confidence", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("analysis", "sponsor_evidence", "TEXT"),
    ("analysis", "blocking_groups_json", "TEXT NOT NULL DEFAULT '[]'"),
]


def init_db(conn: sqlite3.Connection) -> None:
    """Execute `SCHEMA`, then add any column a pre-existing table is missing. Idempotent."""
    conn.executescript(SCHEMA)
    for table, column, decl in ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    # A pre-addendum `postings` table still carries `alerted_at`. It is left in place
    # (SQLite drops columns only on new-ish versions, and nothing reads it any more); the
    # ledger migration in `ledger.migrate_legacy` is what carries its content across.
    conn.commit()


def begin_run(conn: sqlite3.Connection, at: datetime | None = None) -> int:
    """Start a run and return its id. Repost detection compares against this."""
    at = at or datetime.now(UTC)
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_iso(at),))
    conn.commit()
    return int(cur.lastrowid)


# --- postings -----------------------------------------------------------------------

def upsert_posting(
    conn: sqlite3.Connection, posting: Posting, run_id: int | None = None
) -> str:
    """Insert or refresh one posting; return what happened.

    Returns one of "new" | "seen" | "duplicate" | "repost". Only "new" may alert.

      Primary   - exact `global_id` match: update `last_seen_at`, DO NOT re-alert.
      Secondary - fuzzy (normalized_company, normalized_title, first_location) within 60
                  days: store it, write a `duplicates` row so churn is visible, no alert.
      Repost    - previously seen, but not in the immediately preceding run: bump
                  `reposted_count`. Repeatedly reposted reqs are often ghost jobs.

    Content hashing is deliberately NOT the dedup key: reposts and title edits defeat it.
    """
    if run_id is None:
        run_id = _current_run(conn)
    norm_company = normalize_company(posting.company)
    norm_title = normalize_title(posting.title)
    first_location = posting.locations[0] if posting.locations else None

    row = conn.execute(
        "SELECT first_seen_at, reposted_count, last_seen_run FROM postings WHERE global_id = ?",
        (posting.global_id,),
    ).fetchone()

    if row is not None:
        previous_run = row["last_seen_run"]
        reposted = row["reposted_count"]
        outcome = "seen"
        if previous_run is not None and run_id - previous_run > 1:
            reposted += 1
            outcome = "repost"
        conn.execute(
            """UPDATE postings SET last_seen_at = ?, description_html = ?,
                   description_text = ?, title = ?, normalized_title = ?, apply_url = ?,
                   locations_json = ?, first_location = ?, raw_json = ?,
                   reposted_count = ?, last_seen_run = ?
               WHERE global_id = ?""",
            (
                _iso(posting.last_seen_at), posting.description_html,
                posting.description_text, posting.title, norm_title, posting.apply_url,
                json.dumps(posting.locations), first_location, json.dumps(posting.raw),
                reposted, run_id, posting.global_id,
            ),
        )
        conn.commit()
        return outcome

    matched = find_fuzzy_duplicate(conn, posting)
    conn.execute(
        """INSERT INTO postings (
               global_id, source, company, normalized_company, title, normalized_title,
               first_location, locations_json, remote_type, description_html,
               description_text, apply_url, posted_at, first_seen_at, last_seen_at,
               raw_json, locale, reposted_count, last_seen_run)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
        (
            posting.global_id, posting.source, posting.company, norm_company,
            posting.title, norm_title, first_location, json.dumps(posting.locations),
            posting.remote_type, posting.description_html, posting.description_text,
            posting.apply_url, _iso(posting.posted_at), _iso(posting.first_seen_at),
            _iso(posting.last_seen_at), json.dumps(posting.raw), posting.locale, run_id,
        ),
    )
    if matched:
        conn.execute(
            """INSERT OR IGNORE INTO duplicates (global_id, matched_global_id, matched_on,
                                                 seen_at) VALUES (?,?,?,?)""",
            (posting.global_id, matched, "company+title+location",
             _iso(posting.first_seen_at)),
        )
        conn.commit()
        return "duplicate"
    conn.commit()
    return "new"


def find_fuzzy_duplicate(
    conn: sqlite3.Connection, posting: Posting, within_days: int = FUZZY_WINDOW_DAYS
) -> str | None:
    """Secondary dedup lookup. Returns the matched `global_id`, or None.

    Exact match on (normalized_company, normalized_title, first_location). The company is
    part of the key on purpose: the same title at two different employers is two jobs, and
    both must alert (T3.7). Resist adding edit distance until a real miss justifies it.
    """
    cutoff = _iso(posting.first_seen_at - timedelta(days=within_days))
    row = conn.execute(
        """SELECT global_id FROM postings
           WHERE normalized_company = ? AND normalized_title = ?
             AND first_location IS ? AND first_seen_at >= ? AND global_id != ?
           ORDER BY first_seen_at DESC LIMIT 1""",
        (
            normalize_company(posting.company), normalize_title(posting.title),
            posting.locations[0] if posting.locations else None, cutoff, posting.global_id,
        ),
    ).fetchone()
    return row["global_id"] if row else None


def get_posting(conn: sqlite3.Connection, global_id: str) -> Posting | None:
    row = conn.execute("SELECT * FROM postings WHERE global_id = ?", (global_id,)).fetchone()
    return _row_to_posting(row) if row else None


def iter_postings(
    conn: sqlite3.Connection,
    since: datetime | None = None,
    include_blocked: bool = False,
    company: str | None = None,
) -> Iterator[tuple[Posting, Analysis | None]]:
    """Query postings with their analysis.

    `include_blocked=False` hides eligibility='blocked'; `--show-blocked` flips it. Blocked
    postings are filtered at the *presentation* layer only - never absent from the DB.
    """
    sql = ["SELECT p.*, a.global_id AS a_id FROM postings p",
           "LEFT JOIN analysis a ON a.global_id = p.global_id WHERE 1=1"]
    args: list[object] = []
    if since is not None:
        sql.append("AND p.first_seen_at >= ?")
        args.append(_iso(since))
    if company:
        sql.append("AND p.normalized_company = ?")
        args.append(normalize_company(company))
    if not include_blocked:
        sql.append("AND (a.eligibility IS NULL OR a.eligibility != 'blocked')")
    sql.append("ORDER BY p.first_seen_at DESC")
    for row in conn.execute(" ".join(sql), args):
        yield _row_to_posting(row), get_analysis(conn, row["global_id"])


def unalerted_postings(
    conn: sqlite3.Connection, run_id: int, profile: str = DEFAULT_PROFILE
) -> list[Posting]:
    """Postings seen in this run that have never been delivered TO THIS PROFILE.

    This, not "the upsert said new", is what drives alerting. A posting whose notification
    failed last run is stored and no longer new, so keying off the upsert outcome would
    lose it permanently - the exact failure `_send` claims not to have. Duplicates and
    reposts are excluded here because they were alerted under their original global_id.

    Addendum 2, build step 1: the NOT EXISTS is scoped to one profile. Previously this
    read `postings.alerted_at IS NULL`, a single global flag, so the first profile to
    alert on a posting silenced every other profile for good (T8.1).
    """
    rows = conn.execute(
        """SELECT p.* FROM postings p
           LEFT JOIN duplicates d ON d.global_id = p.global_id
           WHERE p.last_seen_run = ? AND d.global_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM alerts a
                             WHERE a.global_id = p.global_id AND a.profile = ?)
           ORDER BY p.first_seen_at""",
        (run_id, profile),
    ).fetchall()
    return [_row_to_posting(r) for r in rows]


def mark_alerted(
    conn: sqlite3.Connection,
    global_ids: list[str],
    at: datetime,
    profile: str = DEFAULT_PROFILE,
    channel: str = "digest",
    scores: dict[str, float] | None = None,
) -> None:
    """Record that these went out for one profile. Called AFTER the notifier confirms
    delivery, never before: a commit ahead of a failed notify loses that alert permanently.

    `INSERT OR IGNORE` rather than a plain insert: re-delivering a posting that is already
    recorded for this profile is a no-op, not a crash. The first delivery's timestamp is
    the one worth keeping.
    """
    scores = scores or {}
    conn.executemany(
        """INSERT OR IGNORE INTO alerts (global_id, profile, alerted_at, channel, score)
           VALUES (?,?,?,?,?)""",
        [(gid, profile, _iso(at), channel, scores.get(gid)) for gid in global_ids],
    )
    conn.commit()


def alerted_at(
    conn: sqlite3.Connection, global_id: str, profile: str = DEFAULT_PROFILE
) -> datetime | None:
    """When this profile was told about this posting, or None."""
    row = conn.execute(
        "SELECT alerted_at FROM alerts WHERE global_id = ? AND profile = ?",
        (global_id, profile),
    ).fetchone()
    return datetime.fromisoformat(row["alerted_at"]) if row else None


def alerts_since(
    conn: sqlite3.Connection, since: datetime, profile: str | None = None
) -> list[sqlite3.Row]:
    """Alert rows delivered at or after `since`, newest first.

    Feeds both `jobmon audit` (addendum 1) and the per-day caps in `limits` (addendum 5):
    "how many instants have I already sent today" is a question about this table and
    nothing else.
    """
    sql = ["SELECT * FROM alerts WHERE alerted_at >= ?"]
    args: list[object] = [_iso(since)]
    if profile is not None:
        sql.append("AND profile = ?")
        args.append(profile)
    sql.append("ORDER BY alerted_at DESC")
    return conn.execute(" ".join(sql), args).fetchall()


def count_alerts_since(
    conn: sqlite3.Connection,
    since: datetime,
    profile: str,
    channel: str | None = None,
    company: str | None = None,
) -> int:
    """How many alerts this profile has already sent in the window. Drives
    `max_instant_per_day` and `max_per_company_per_day` (addendum 5).

    The company filter joins back to `postings` and compares normalized names, so
    "Acme, Inc." and "acme" count against one cap rather than two.
    """
    sql = ["SELECT COUNT(*) AS n FROM alerts a",
           "JOIN postings p ON p.global_id = a.global_id",
           "WHERE a.alerted_at >= ? AND a.profile = ?"]
    args: list[object] = [_iso(since), profile]
    if channel is not None:
        sql.append("AND a.channel = ?")
        args.append(channel)
    if company is not None:
        sql.append("AND p.normalized_company = ?")
        args.append(normalize_company(company))
    row = conn.execute(" ".join(sql), args).fetchone()
    return int(row["n"]) if row else 0


def alerts_by_company_since(
    conn: sqlite3.Connection, since: datetime, profile: str
) -> dict[str, int]:
    """Normalized company -> alerts already sent to this profile in the window.

    Seeds `max_per_company_per_day` across an hourly cron: without it a per-DAY cap would
    reset twenty-four times a day and cap nothing.
    """
    rows = conn.execute(
        """SELECT p.normalized_company AS company, COUNT(*) AS n
           FROM alerts a JOIN postings p ON p.global_id = a.global_id
           WHERE a.alerted_at >= ? AND a.profile = ?
           GROUP BY p.normalized_company""",
        (_iso(since), profile),
    ).fetchall()
    return {r["company"]: int(r["n"]) for r in rows}


def reposted_ids(conn: sqlite3.Connection, minimum: int = 1) -> set[str]:
    """Postings the store has seen disappear and come back at least `minimum` times.

    `Posting` deliberately carries no store bookkeeping - it is what an adapter produces -
    so `limits.suppress_reposts` asks the store rather than reading a field off the record.
    Repeatedly reposted reqs are often ghost jobs.
    """
    return {
        r["global_id"]
        for r in conn.execute(
            "SELECT global_id FROM postings WHERE reposted_count >= ?", (minimum,)
        )
    }


def applied_ids(conn: sqlite3.Connection) -> set[str]:
    """Postings you have recorded an application against. `suppress_if_applied` (T8.8)."""
    return {r["global_id"] for r in conn.execute("SELECT global_id FROM applications")}


# --- analysis -----------------------------------------------------------------------

def upsert_analysis(conn: sqlite3.Connection, analysis: Analysis) -> None:
    """Write the derived record. Re-analyzing an existing posting (after a taxonomy edit)
    overwrites in place and does not touch `alerted_at`, so it cannot trigger a re-alert."""
    conn.execute(
        """INSERT INTO analysis (global_id, role_class, function_json, eligibility,
               eligibility_evidence_json, grad_window, keywords_json, gaps_json,
               recommended_variant, variant_scores_json, degree_gated, yoe_floor,
               text_eligibility, sponsor_confidence, sponsor_evidence,
               blocking_groups_json, analyzed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(global_id) DO UPDATE SET
               role_class=excluded.role_class, function_json=excluded.function_json,
               eligibility=excluded.eligibility,
               eligibility_evidence_json=excluded.eligibility_evidence_json,
               grad_window=excluded.grad_window, keywords_json=excluded.keywords_json,
               gaps_json=excluded.gaps_json,
               recommended_variant=excluded.recommended_variant,
               variant_scores_json=excluded.variant_scores_json,
               degree_gated=excluded.degree_gated, yoe_floor=excluded.yoe_floor,
               text_eligibility=excluded.text_eligibility,
               sponsor_confidence=excluded.sponsor_confidence,
               sponsor_evidence=excluded.sponsor_evidence,
               blocking_groups_json=excluded.blocking_groups_json,
               analyzed_at=excluded.analyzed_at""",
        (
            analysis.global_id, analysis.role_class, json.dumps(analysis.function),
            analysis.eligibility, json.dumps(analysis.eligibility_evidence),
            analysis.grad_window, json.dumps(analysis.keywords), json.dumps(analysis.gaps),
            analysis.recommended_variant, json.dumps(analysis.variant_scores),
            int(analysis.degree_gated), analysis.yoe_floor, analysis.text_eligibility,
            analysis.sponsor_confidence, analysis.sponsor_evidence,
            json.dumps(analysis.blocking_groups), _iso(datetime.now(UTC)),
        ),
    )
    conn.commit()


def get_analysis(conn: sqlite3.Connection, global_id: str) -> Analysis | None:
    row = conn.execute("SELECT * FROM analysis WHERE global_id = ?", (global_id,)).fetchone()
    if row is None:
        return None
    return Analysis(
        global_id=row["global_id"],
        role_class=row["role_class"],
        function=json.loads(row["function_json"]),
        eligibility=row["eligibility"],
        eligibility_evidence=json.loads(row["eligibility_evidence_json"]),
        keywords=json.loads(row["keywords_json"]),
        gaps=json.loads(row["gaps_json"]),
        recommended_variant=row["recommended_variant"],
        variant_scores=json.loads(row["variant_scores_json"]),
        grad_window=row["grad_window"],
        degree_gated=bool(row["degree_gated"]),
        yoe_floor=row["yoe_floor"],
        text_eligibility=row["text_eligibility"],
        sponsor_confidence=row["sponsor_confidence"],
        sponsor_evidence=row["sponsor_evidence"],
        blocking_groups=json.loads(row["blocking_groups_json"]),
    )


# --- application tracking (spec 11, build step 7a) ----------------------------------
#
# This is the tracker half of the project (spec 11): the monitor already has every
# posting record, so `apply`/`log` just add a fact on top of one, and `funnel` is the one
# report that decides whether the volume strategy is working. Nothing here alerts,
# schedules, or touches the ledger - it is pure bookkeeping on rows the pipeline already
# wrote.

CALLBACK_STATUSES = frozenset({"screen", "onsite", "offer"})  # a real response, not silence


class ApplicationError(ValueError):
    """`apply`/`log` cannot proceed cleanly: unknown `global_id`, or a posting already
    marked applied. Raised instead of letting a raw `sqlite3.IntegrityError` surface, so
    the CLI can print something a mistyped global_id actually explains (T7.1)."""


def record_application(
    conn: sqlite3.Connection, global_id: str, variant: str, at: datetime, note: str | None = None
) -> None:
    """`jobmon apply <global_id> --variant mle` (spec 1, 11).

    Refuses a second `apply` on the same posting rather than silently no-op-ing or adding a
    second row (T7.2): `funnel` counts `applications` 1:1 against `postings`, so a duplicate
    row would inflate "applications sent" and quietly skew the one number the report exists
    to protect (spec 11) - a fat-fingered repeat of the same global_id should be visible,
    not swallowed.
    """
    if get_posting(conn, global_id) is None:
        raise ApplicationError(f"no posting with global_id {global_id!r}")
    existing = conn.execute(
        "SELECT applied_at, variant FROM applications WHERE global_id = ?", (global_id,)
    ).fetchone()
    if existing is not None:
        raise ApplicationError(
            f"{global_id} was already marked applied on {existing['applied_at']} "
            f"(variant {existing['variant']!r}) - not recorded again"
        )
    conn.execute(
        "INSERT INTO applications (global_id, variant, applied_at, note) VALUES (?,?,?,?)",
        (global_id, variant, _iso(at), note),
    )
    conn.commit()


def record_event(
    conn: sqlite3.Connection, global_id: str, status: ApplicationStatus, at: datetime,
    note: str | None = None,
) -> None:
    """`jobmon log <global_id> --status screen|reject|onsite|offer` (spec 11).

    Append-only: `application_events` has no primary key on `global_id`, so a posting can
    move through several stages (screen, then onsite, then offer) and every one is kept -
    `funnel` reads "did this posting ever reach a callback status", not just the latest one.

    Requires a prior `apply`: advancing a funnel stage for a posting you never recorded as
    applied-to is almost certainly the wrong global_id, and failing here is cheaper than a
    `funnel` report that counts a callback against zero applications.
    """
    if get_posting(conn, global_id) is None:
        raise ApplicationError(f"no posting with global_id {global_id!r}")
    applied = conn.execute(
        "SELECT 1 FROM applications WHERE global_id = ?", (global_id,)
    ).fetchone()
    if applied is None:
        raise ApplicationError(
            f"{global_id} has no recorded application - `jobmon apply` it first"
        )
    conn.execute(
        "INSERT INTO application_events (global_id, status, occurred_at, note) "
        "VALUES (?,?,?,?)",
        (global_id, status, _iso(at), note),
    )
    conn.commit()


def funnel_report(conn: sqlite3.Connection) -> dict[str, object]:
    """Applications sent, callbacks, and conversion rate, overall and split by variant and
    by function (spec 11) - the number that decides whether the volume strategy is
    working, not just a count of what got sent.

    A "callback" is a posting with at least one event in `CALLBACK_STATUSES` - a straight
    `reject` with nothing else logged is not one. Counted per distinct posting, not per
    event, so logging screen-then-onsite for one posting is one callback, not two.

    `function` is multi-label (spec 4: `["swe", "mle"]` is normal), so one application can
    count toward more than one function bucket; that mirrors how keywords and gaps already
    treat it, and a single split-by-function total is not expected to sum to
    `applications_sent`.
    """
    applications = conn.execute(
        "SELECT global_id, variant FROM applications"
    ).fetchall()
    if not applications:
        return {
            "applications_sent": 0,
            "callbacks": 0,
            "conversion_rate": 0.0,
            "by_variant": {},
            "by_function": {},
        }

    callback_ids = {
        row["global_id"]
        for row in conn.execute(
            f"SELECT DISTINCT global_id FROM application_events "
            f"WHERE status IN ({','.join('?' * len(CALLBACK_STATUSES))})",
            tuple(CALLBACK_STATUSES),
        ).fetchall()
    }

    by_variant: dict[str, list[int]] = {}  # variant -> [applied, callbacks]
    by_function: dict[str, list[int]] = {}
    for row in applications:
        gid, variant = row["global_id"], row["variant"]
        got_callback = gid in callback_ids
        bucket = by_variant.setdefault(variant, [0, 0])
        bucket[0] += 1
        bucket[1] += int(got_callback)

        analysis = get_analysis(conn, gid)
        for fn in (analysis.function if analysis else []):
            bucket = by_function.setdefault(fn, [0, 0])
            bucket[0] += 1
            bucket[1] += int(got_callback)

    def _rate(applied: int, callbacks: int) -> float:
        return round(callbacks / applied * 100, 1) if applied else 0.0

    total_applied = len(applications)
    total_callbacks = sum(1 for row in applications if row["global_id"] in callback_ids)
    return {
        "applications_sent": total_applied,
        "callbacks": total_callbacks,
        "conversion_rate": _rate(total_applied, total_callbacks),
        "by_variant": {
            v: {"applied": a, "callbacks": c, "conversion_rate": _rate(a, c)}
            for v, (a, c) in by_variant.items()
        },
        "by_function": {
            f: {"applied": a, "callbacks": c, "conversion_rate": _rate(a, c)}
            for f, (a, c) in by_function.items()
        },
    }


# --- adapter health (spec 5b, 10) ---------------------------------------------------

def record_health(conn: sqlite3.Connection, health: AdapterHealth) -> None:
    """One row per adapter per run."""
    conn.execute(
        """INSERT INTO adapter_health (adapter, run_at, ok, postings_seen, new_postings,
               duration_ms, error) VALUES (?,?,?,?,?,?,?)""",
        (
            health.adapter, _iso(health.run_at), int(health.ok), health.postings_seen,
            health.new_postings, health.duration_ms, health.error,
        ),
    )
    conn.commit()


def health_report(conn: sqlite3.Connection, runs: int = 8) -> list[AdapterHealth]:
    """Recent per-adapter health, newest first.

    Zero postings with no error, repeatedly, is silent breakage - the failure mode that
    actually happens. The caller compares consecutive rows; this only supplies them.
    """
    rows = conn.execute(
        "SELECT * FROM adapter_health ORDER BY run_at DESC, id DESC LIMIT ?", (runs,)
    ).fetchall()
    return [
        AdapterHealth(
            adapter=r["adapter"],
            run_at=datetime.fromisoformat(r["run_at"]),
            ok=bool(r["ok"]),
            postings_seen=r["postings_seen"],
            new_postings=r["new_postings"],
            duration_ms=r["duration_ms"],
            error=r["error"],
        )
        for r in rows
    ]


# --- helpers ------------------------------------------------------------------------

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _current_run(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(id) AS n FROM runs").fetchone()
    return int(row["n"]) if row and row["n"] is not None else begin_run(conn)


def _row_to_posting(row: sqlite3.Row) -> Posting:
    return Posting(
        global_id=row["global_id"],
        source=row["source"],
        company=row["company"],
        title=row["title"],
        locations=json.loads(row["locations_json"]),
        description_html=row["description_html"],
        description_text=row["description_text"],
        apply_url=row["apply_url"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        remote_type=row["remote_type"],
        posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
        raw=json.loads(row["raw_json"]),
        locale=row["locale"],
    )
