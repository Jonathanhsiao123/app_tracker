"""Committed ledger: the only durable state (build step 6).

SQLite is demoted to a derived cache, rebuilt from this file at the start of every run and
thrown away after. The ledger is committed; the `.db` never is.

`actions/cache` is deliberately NOT used. Entries evict silently after 7 days idle and the
failure mode is a mass re-alert - the loudest possible failure for the quietest possible
cause.

FIELD SET. Build step 6 specifies four fields per line: `global_id`, `first_seen_at`,
`alerted_at`, `reposted_count`. Those four are not sufficient, and the extra ones here are
not decoration:

  * `company` / `normalized_title` / `first_location` - the secondary dedup key (spec 4).
    The cache is rebuilt every run, so with only `global_id` the fuzzy 60-day match would
    have no history to match against and T3.5 would never fire again.
  * `last_seen_run` - repost detection is "seen before, but not in the previous run"
    (T3.6). Without a run counter that is unanswerable after a rebuild.
  * `title` / `source` / `apply_url` - so a rebuilt row is legible in `jobmon list`
    instead of a blank placeholder.

Descriptions are NOT stored. They are the bulk of a posting and they come back on the next
fetch; keeping them would make a committed file grow by megabytes per run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

LEDGER_PATH = Path("state/seen.jsonl")
HEALTH_PATH = Path("state/health.json")


@dataclass(slots=True)
class Entry:
    """One posting's durable state. One JSON object per line."""

    global_id: str
    source: str = ""
    company: str = ""
    title: str = ""
    normalized_title: str = ""
    first_location: str | None = None
    apply_url: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    last_seen_run: int = 0
    alerted_at: str | None = None
    reposted_count: int = 0


@dataclass(slots=True)
class Canary:
    """A per-adapter zero-result regression (build step 6.4)."""

    adapter: str
    previous: int
    current: int

    def __str__(self) -> str:
        return f"{self.adapter}: {self.previous} postings last run, {self.current} now"


@dataclass(slots=True)
class Health:
    """Per-adapter counts from the previous run, plus the run counter.

    Kept beside the ledger because the SQLite `adapter_health` table dies with the derived
    cache. `last_run_id` lives here rather than being derived from the ledger because a run
    that sees NO postings advances no posting's `last_seen_run` - so deriving the counter
    from the ledger would stall it, and repost detection ("seen before, but not in the
    previous run") would quietly stop firing. That is exactly T6.6's scenario.
    """

    counts: dict[str, int] = field(default_factory=dict)
    last_run_id: int = 0


# --- read / write -------------------------------------------------------------------

def load(path: Path = LEDGER_PATH) -> dict[str, Entry]:
    """Read the ledger. A missing file is an empty ledger, not an error - that is the
    first-ever run.

    A malformed line is skipped with a warning rather than aborting the run: losing one
    posting's history costs a duplicate alert, while refusing to start costs every alert.
    """
    if not path.exists():
        return {}
    entries: dict[str, Entry] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            entry = Entry(**data)
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("ledger %s line %d is unreadable, skipping: %s", path, lineno, exc)
            continue
        entries[entry.global_id] = entry
    return entries


def save(entries: dict[str, Entry], path: Path = LEDGER_PATH) -> None:
    """Write the whole ledger, one line per unique `global_id`, sorted for a stable diff.

    A full rewrite rather than an append: appending would let one `global_id` appear on
    several lines, and T6.9 requires exactly one. Sorting keeps the committed diff to the
    lines that actually changed instead of reshuffling the file every run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(asdict(entries[gid]), sort_keys=True, separators=(",", ":"))
        for gid in sorted(entries)
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def next_run_id(entries: dict[str, Entry], health: Health | None = None) -> int:
    """The next run's id. Monotonic across rebuilds.

    Taken from the health file, which is written every run whether or not anything was
    seen. The ledger is only the fallback for an upgrade from a pre-health state.
    """
    from_ledger = max((e.last_seen_run for e in entries.values()), default=0)
    from_health = health.last_run_id if health else 0
    return max(from_ledger, from_health) + 1


# --- cache rebuild ------------------------------------------------------------------

def rebuild(conn: sqlite3.Connection, entries: dict[str, Entry]) -> int:
    """Seed the derived SQLite cache from the ledger. Returns the row count.

    Only the dedup-relevant columns are populated; `description_html` and
    `description_text` are empty until this run's fetch fills them in. `upsert_posting`
    overwrites those on sight, so a posting still live gets its real content back
    immediately and one that has closed keeps a legible-but-bodyless row.
    """
    rows = [
        (
            e.global_id, e.source or "ledger", e.company,
            _normalize_company(e.company), e.title, e.normalized_title,
            e.first_location, "[]", None, "", "",
            e.apply_url, None, e.first_seen_at, e.last_seen_at,
            "{}", "US", e.reposted_count, e.alerted_at, e.last_seen_run,
        )
        for e in entries.values()
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO postings (
               global_id, source, company, normalized_company, title, normalized_title,
               first_location, locations_json, remote_type, description_html,
               description_text, apply_url, posted_at, first_seen_at, last_seen_at,
               raw_json, locale, reposted_count, alerted_at, last_seen_run)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    # The runs table drives nothing but sequencing; seed it so run ids stay monotonic.
    highest = next_run_id(entries) - 1
    if highest > 0:
        conn.execute(
            "INSERT OR REPLACE INTO runs (id, started_at) VALUES (?, ?)",
            (highest, entries[next(iter(entries))].last_seen_at or ""),
        )
    conn.commit()
    return len(rows)


def _normalize_company(name: str) -> str:
    from .normalize import normalize_company

    return normalize_company(name)


def from_db(conn: sqlite3.Connection) -> dict[str, Entry]:
    """Snapshot the cache back into ledger entries. Called AFTER notification."""
    rows = conn.execute(
        """SELECT global_id, source, company, title, normalized_title, first_location,
                  apply_url, first_seen_at, last_seen_at, last_seen_run, alerted_at,
                  reposted_count
           FROM postings"""
    ).fetchall()
    return {
        r["global_id"]: Entry(
            global_id=r["global_id"],
            source=r["source"],
            company=r["company"],
            title=r["title"],
            normalized_title=r["normalized_title"],
            first_location=r["first_location"],
            apply_url=r["apply_url"],
            first_seen_at=r["first_seen_at"] or "",
            last_seen_at=r["last_seen_at"] or "",
            last_seen_run=r["last_seen_run"] or 0,
            alerted_at=r["alerted_at"],
            reposted_count=r["reposted_count"] or 0,
        )
        for r in rows
    }


# --- health, for the canary ---------------------------------------------------------

def load_health(path: Path = HEALTH_PATH) -> Health:
    if not path.exists():
        return Health()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Health(
            counts={str(k): int(v) for k, v in (data.get("counts") or {}).items()},
            last_run_id=int(data.get("last_run_id") or 0),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("health file %s unreadable, treating as empty: %s", path, exc)
        return Health()


def save_health(health: Health, path: Path = HEALTH_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"counts": health.counts, "last_run_id": health.last_run_id},
            sort_keys=True,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )


def check_canary(previous: Health, current: dict[str, int]) -> list[Canary]:
    """Adapters that returned postings last run and none this run (build step 6.4).

    An adapter that returned 0 last run and 0 now is NOT a regression (T6.6) - it is a
    board set with nothing open, or an adapter that has been broken long enough to be old
    news. Only the transition from some to none is the alarm.
    """
    return [
        Canary(adapter=name, previous=previous.counts.get(name, 0), current=count)
        for name, count in current.items()
        if previous.counts.get(name, 0) > 0 and count == 0
    ]
