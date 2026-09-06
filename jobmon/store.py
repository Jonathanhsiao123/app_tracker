"""SQLite storage and dedup (spec 4). PHASE 1; application tracking tables are PHASE 3.

One file, committed nowhere. No ORM, no migrations framework - `SCHEMA` is executed with
`CREATE TABLE IF NOT EXISTS` and altered by hand if it ever needs to change.

Nothing is ever deleted. `blocked` postings are stored and shown by `--show-blocked`;
closed and duplicate postings are stored so churn is visible.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .models import Analysis, AdapterHealth, ApplicationStatus, Posting

DB_PATH = Path("jobmon.db")

SCHEMA = """
-- TODO: fill in. Sketch of the tables the spec requires:
--
-- postings(global_id PK, source, company, title, locations_json, remote_type,
--          description_html, description_text, apply_url, posted_at, first_seen_at,
--          last_seen_at, raw_json, locale, reposted_count DEFAULT 0, alerted_at)
--   index on (company, normalized_title, first_location) for the secondary dedup pass
--   index on first_seen_at for `jobmon list --since`
--
-- analysis(global_id PK REFERENCES postings, role_class, function_json, eligibility,
--          eligibility_evidence_json, grad_window, keywords_json, gaps_json,
--          recommended_variant, variant_scores_json, degree_gated, yoe_floor,
--          analyzed_at)
--
-- applications(global_id PK REFERENCES postings, variant, applied_at, note)
-- application_events(id PK, global_id, status, occurred_at, note)   -- the funnel
--
-- adapter_health(id PK, adapter, run_at, ok, postings_seen, new_postings, duration_ms, error)
--
-- duplicates(global_id, matched_global_id, matched_on, seen_at)  -- suppressed alerts
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open the database, enabling foreign keys and WAL. TODO: `row_factory = sqlite3.Row`
    so callers read columns by name."""
    raise NotImplementedError


def init_db(conn: sqlite3.Connection) -> None:
    """Execute `SCHEMA`. Idempotent. TODO: called by `jobmon init-db` and by `run`."""
    raise NotImplementedError


# --- postings -----------------------------------------------------------------------

def upsert_posting(conn: sqlite3.Connection, posting: Posting) -> str:
    """Insert or refresh one posting; return what happened.

    Returns one of "new" | "seen" | "duplicate" | "repost". Dedup rules (spec 4):

      Primary   - exact `global_id` match: update `last_seen_at`, DO NOT re-alert.
      Secondary - fuzzy (company, normalized_title, first_location) within 60 days:
                  suppress the alert but write a `duplicates` row so churn is visible.
      Repost    - a posting that disappeared and came back: bump `reposted_count`.
                  Repeatedly reposted reqs are often ghost jobs or a hard-to-fill team.

    Content hashing is deliberately NOT the dedup key: reposts and title edits defeat it.

    TODO: "disappeared then returned" needs the previous run's seen-set, so the runner has
    to record which global_ids each adapter returned this run, not just the new ones.
    """
    raise NotImplementedError


def find_fuzzy_duplicate(
    conn: sqlite3.Connection, posting: Posting, within_days: int = 60
) -> str | None:
    """Secondary dedup lookup. Returns the matched `global_id`, or None.
    TODO: exact match on (normalized_company, normalized_title, first_location) is enough
    to start. Resist adding edit-distance until a real miss justifies it."""
    raise NotImplementedError


def get_posting(conn: sqlite3.Connection, global_id: str) -> Posting | None:
    """TODO: used by `jobmon show`, `apply`, and `log`."""
    raise NotImplementedError


def iter_postings(
    conn: sqlite3.Connection,
    since: datetime | None = None,
    include_blocked: bool = False,
    company: str | None = None,
) -> Iterator[tuple[Posting, Analysis | None]]:
    """Query postings with their analysis. TODO: `include_blocked=False` hides
    eligibility='blocked' from normal output; `--show-blocked` flips it. Blocked postings
    are filtered at the *presentation* layer only - they are never absent from the DB."""
    raise NotImplementedError


def mark_alerted(conn: sqlite3.Connection, global_ids: list[str], at: datetime) -> None:
    """Record that these went out, so a crash mid-notify does not re-alert on the next run.
    TODO: write this AFTER the notifier confirms delivery, not before."""
    raise NotImplementedError


# --- analysis -----------------------------------------------------------------------

def upsert_analysis(conn: sqlite3.Connection, analysis: Analysis) -> None:
    """Write the derived record. TODO: re-analyzing an existing posting (after a taxonomy
    edit) must overwrite in place and must not trigger a re-alert."""
    raise NotImplementedError


def get_analysis(conn: sqlite3.Connection, global_id: str) -> Analysis | None:
    raise NotImplementedError


# --- application tracking (spec 11, PHASE 3) ----------------------------------------

def record_application(
    conn: sqlite3.Connection, global_id: str, variant: str, at: datetime, note: str | None = None
) -> None:
    """`jobmon apply <global_id> --variant mle`. TODO: reject an unknown global_id loudly,
    and refuse to silently overwrite an existing application row."""
    raise NotImplementedError


def record_event(
    conn: sqlite3.Connection, global_id: str, status: ApplicationStatus, at: datetime,
    note: str | None = None,
) -> None:
    """`jobmon log <global_id> --status screen|reject|onsite|offer`. Append-only: an
    application's history is the event list, so a status is never edited in place."""
    raise NotImplementedError


def funnel_report(conn: sqlite3.Connection) -> dict[str, object]:
    """The one report the tracking exists for (spec 11).

    Applications sent, callbacks, and conversion rate **by variant and by function**. This
    is the number that decides whether the volume strategy is working; without it the whole
    pipeline is a guess. Watch specifically for volume up / conversion zero, which means
    the bottleneck is the technical loop, not the pipeline (spec 14).

    TODO: a callback is any event above `applied` other than `reject`. Report raw counts
    alongside rates - a 50% rate on 2 applications is noise.
    """
    raise NotImplementedError


# --- adapter health (spec 5b, 10) ---------------------------------------------------

def record_health(conn: sqlite3.Connection, health: AdapterHealth) -> None:
    """One row per adapter per run."""
    raise NotImplementedError


def health_report(conn: sqlite3.Connection, runs: int = 8) -> list[AdapterHealth]:
    """Recent per-adapter health. TODO: flag an adapter that returned zero postings on
    every recent run without erroring - that is silent breakage, the failure mode that
    actually happens to big tech portal adapters."""
    raise NotImplementedError
