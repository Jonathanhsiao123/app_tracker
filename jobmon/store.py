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
    alerted_at          TEXT,
    last_seen_run       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_postings_fuzzy
    ON postings (normalized_company, normalized_title, first_location);
CREATE INDEX IF NOT EXISTS idx_postings_first_seen ON postings (first_seen_at);

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


def init_db(conn: sqlite3.Connection) -> None:
    """Execute `SCHEMA`. Idempotent."""
    conn.executescript(SCHEMA)
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
               raw_json, locale, reposted_count, alerted_at, last_seen_run)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?)""",
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


def unalerted_postings(conn: sqlite3.Connection, run_id: int) -> list[Posting]:
    """Postings seen in this run that have never been delivered.

    This, not "the upsert said new", is what drives alerting. A posting whose notification
    failed last run is stored and no longer new, so keying off the upsert outcome would
    lose it permanently - the exact failure `_send` claims not to have. Duplicates and
    reposts are excluded here because they were alerted under their original global_id.
    """
    rows = conn.execute(
        """SELECT p.* FROM postings p
           LEFT JOIN duplicates d ON d.global_id = p.global_id
           WHERE p.last_seen_run = ? AND p.alerted_at IS NULL AND d.global_id IS NULL
           ORDER BY p.first_seen_at""",
        (run_id,),
    ).fetchall()
    return [_row_to_posting(r) for r in rows]


def mark_alerted(conn: sqlite3.Connection, global_ids: list[str], at: datetime) -> None:
    """Record that these went out. Called AFTER the notifier confirms delivery, never
    before: a commit ahead of a failed notify loses that alert permanently."""
    conn.executemany(
        "UPDATE postings SET alerted_at = ? WHERE global_id = ?",
        [(_iso(at), gid) for gid in global_ids],
    )
    conn.commit()


# --- analysis -----------------------------------------------------------------------

def upsert_analysis(conn: sqlite3.Connection, analysis: Analysis) -> None:
    """Write the derived record. Re-analyzing an existing posting (after a taxonomy edit)
    overwrites in place and does not touch `alerted_at`, so it cannot trigger a re-alert."""
    conn.execute(
        """INSERT INTO analysis (global_id, role_class, function_json, eligibility,
               eligibility_evidence_json, grad_window, keywords_json, gaps_json,
               recommended_variant, variant_scores_json, degree_gated, yoe_floor,
               analyzed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(global_id) DO UPDATE SET
               role_class=excluded.role_class, function_json=excluded.function_json,
               eligibility=excluded.eligibility,
               eligibility_evidence_json=excluded.eligibility_evidence_json,
               grad_window=excluded.grad_window, keywords_json=excluded.keywords_json,
               gaps_json=excluded.gaps_json,
               recommended_variant=excluded.recommended_variant,
               variant_scores_json=excluded.variant_scores_json,
               degree_gated=excluded.degree_gated, yoe_floor=excluded.yoe_floor,
               analyzed_at=excluded.analyzed_at""",
        (
            analysis.global_id, analysis.role_class, json.dumps(analysis.function),
            analysis.eligibility, json.dumps(analysis.eligibility_evidence),
            analysis.grad_window, json.dumps(analysis.keywords), json.dumps(analysis.gaps),
            analysis.recommended_variant, json.dumps(analysis.variant_scores),
            int(analysis.degree_gated), analysis.yoe_floor, _iso(datetime.now(UTC)),
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
    )


# --- application tracking (spec 11, PHASE 3) ----------------------------------------

def record_application(
    conn: sqlite3.Connection, global_id: str, variant: str, at: datetime, note: str | None = None
) -> None:
    """`jobmon apply <global_id> --variant mle`. PHASE 3 (build step 7a)."""
    raise NotImplementedError("application tracking is build step 7a")


def record_event(
    conn: sqlite3.Connection, global_id: str, status: ApplicationStatus, at: datetime,
    note: str | None = None,
) -> None:
    """`jobmon log <global_id> --status ...`. PHASE 3 (build step 7a). Append-only."""
    raise NotImplementedError("application tracking is build step 7a")


def funnel_report(conn: sqlite3.Connection) -> dict[str, object]:
    """The one report the tracking exists for (spec 11). PHASE 3 (build step 7a)."""
    raise NotImplementedError("funnel is build step 7a")


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
