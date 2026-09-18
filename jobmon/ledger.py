"""Committed ledger: the only durable state (build step 6; addendum 2, build step 1).

SQLite is demoted to a derived cache, rebuilt from these files at the start of every run
and thrown away after. The ledger is committed; the `.db` never is.

`actions/cache` is deliberately NOT used. Entries evict silently after 7 days idle and the
failure mode is a mass re-alert - the loudest possible failure for the quietest possible
cause.

TWO FILES, NOT ONE (addendum 2). `state/seen.jsonl` conflated "we have this posting" with
"we alerted about it" in a single `alerted_at` field. With profiles those are different
facts, and keeping them in one field means the first profile to alert on a posting silences
every other profile permanently. So:

    state/postings.jsonl          global:      one line per posting, no alert state
    state/alerted/<profile>.jsonl per profile: one line per delivered alert

`migrate_legacy` reads an old `seen.jsonl` once and writes both. It is the first thing
`load_dir` does, and it leaves the old file on disk: a migration that deletes the only copy
of the durable state is not a migration worth running unattended.

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
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATE_DIR = Path("state")
POSTINGS_PATH = STATE_DIR / "postings.jsonl"
ALERTED_DIR = STATE_DIR / "alerted"
HEALTH_PATH = STATE_DIR / "health.json"

# Pre-addendum single file. Read once by `migrate_legacy`, never written again.
LEGACY_PATH = STATE_DIR / "seen.jsonl"

# Kept as an alias because `cli.py --ledger` and `monitor.yml` both name a path, and an
# existing checkout names this one. `resolve_state_dir` turns either spelling into the
# directory the two real files live in.
LEDGER_PATH = POSTINGS_PATH


@dataclass(slots=True)
class Entry:
    """One posting's durable state. One JSON object per line of `postings.jsonl`.

    There is deliberately no `alerted_at` here any more. That field is what made the file
    per-user, and alert state now lives in `alerted/<profile>.jsonl` (addendum 2).
    """

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
    reposted_count: int = 0


@dataclass(slots=True)
class Alert:
    """One delivered alert, for one profile. One JSON object per line of
    `alerted/<profile>.jsonl`.

    `channel` and `score` are here rather than derivable because `jobmon audit` reports on
    what was actually sent, and a score recomputed later against an edited profile is a
    different number from the one that made the routing decision at the time.
    """

    global_id: str
    alerted_at: str
    channel: str = "digest"
    score: float | None = None


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
    # When the previous run finished, so the next one can say how long the gap was.
    #
    # This exists because a dropped schedule is INVISIBLE from the inbox. GitHub delivers
    # `schedule` on a best-effort basis and drops ticks under load; on this repo a nominally
    # hourly cron produced run ids 23 through 33 over 37 hours - ten runs where there should
    # have been thirty-seven, contiguous ids throughout, so nothing crashed and nothing
    # showed up red. The only symptom was mail that felt irregular, which is not a symptom
    # anyone can act on. Stamping the gap into the digest turns it into one.
    last_run_at: str | None = None


@dataclass(slots=True)
class State:
    """Everything the durable files hold: postings, and per-profile alert history."""

    entries: dict[str, Entry] = field(default_factory=dict)
    alerts: dict[str, dict[str, Alert]] = field(default_factory=dict)  # profile -> gid -> Alert


# --- paths ---------------------------------------------------------------------------

def resolve_state_dir(path: Path | None) -> Path:
    """The directory the ledger files live in, from whatever the caller named.

    Accepts the directory itself, `postings.jsonl`, or the pre-addendum `seen.jsonl` - the
    `--ledger state/seen.jsonl` spelling is in an existing workflow file and in muscle
    memory, and breaking it would fail a cron run for a rename.
    """
    if path is None:
        return STATE_DIR
    return path if path.is_dir() or path.suffix == "" else path.parent


def postings_path(state_dir: Path) -> Path:
    return state_dir / POSTINGS_PATH.name


def alerted_path(state_dir: Path, profile: str) -> Path:
    return state_dir / ALERTED_DIR.name / f"{profile}.jsonl"


# --- read / write -------------------------------------------------------------------

def _from_json(cls, data: dict) -> object:
    """Build a dataclass from a JSON object, dropping keys it does not declare.

    Unknown keys are dropped rather than raising so that reading a file written by a newer
    version costs nothing, and so the legacy `alerted_at` field does not break `Entry`.
    """
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _read_jsonl(path: Path, cls) -> list[object]:
    """Read one JSONL file into dataclasses.

    A malformed line is skipped with a warning rather than aborting the run: losing one
    posting's history costs a duplicate alert, while refusing to start costs every alert.
    """
    if not path.exists():
        return []
    out = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(_from_json(cls, json.loads(line)))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("ledger %s line %d is unreadable, skipping: %s", path, lineno, exc)
    return out


def _write_jsonl(path: Path, records: list[object]) -> None:
    """Write the whole file, sorted for a stable diff.

    A full rewrite rather than an append: appending would let one `global_id` appear on
    several lines, and T6.9 requires exactly one. Sorting keeps the committed diff to the
    lines that actually changed instead of reshuffling the file every run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = sorted(
        json.dumps(asdict(r), sort_keys=True, separators=(",", ":")) for r in records
    )
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def load(path: Path = POSTINGS_PATH) -> dict[str, Entry]:
    """Read the postings ledger. A missing file is an empty ledger, not an error - that is
    the first-ever run."""
    return {e.global_id: e for e in _read_jsonl(path, Entry)}  # type: ignore[attr-defined]


def save(entries: dict[str, Entry], path: Path = POSTINGS_PATH) -> None:
    """Write the whole postings ledger, one line per unique `global_id`."""
    _write_jsonl(path, list(entries.values()))


def load_alerts(state_dir: Path = STATE_DIR) -> dict[str, dict[str, Alert]]:
    """Every profile's alert history, keyed profile -> global_id -> Alert.

    Profiles are discovered from the files present, not from the profile config: a profile
    you deleted from `config/profiles/` must still not be re-alerted if you put it back.
    """
    out: dict[str, dict[str, Alert]] = {}
    directory = state_dir / ALERTED_DIR.name
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.jsonl")):
        records = _read_jsonl(path, Alert)
        out[path.stem] = {a.global_id: a for a in records}  # type: ignore[attr-defined]
    return out


def save_alerts(alerts: dict[str, dict[str, Alert]], state_dir: Path = STATE_DIR) -> None:
    """Write one file per profile. A profile with no alerts still gets an (empty) file, so
    that "this profile has never alerted" and "this profile does not exist" stay
    distinguishable in the checkout."""
    for profile, by_id in alerts.items():
        _write_jsonl(alerted_path(state_dir, profile), list(by_id.values()))


def load_state(state_dir: Path = STATE_DIR) -> State:
    """Read both files, migrating a pre-addendum `seen.jsonl` first if that is all there is."""
    migrate_legacy(state_dir)
    return State(
        entries=load(postings_path(state_dir)),
        alerts=load_alerts(state_dir),
    )


def save_state(state: State, state_dir: Path = STATE_DIR) -> None:
    save(state.entries, postings_path(state_dir))
    save_alerts(state.alerts, state_dir)


# --- migration ------------------------------------------------------------------------

def migrate_legacy(
    state_dir: Path = STATE_DIR, profile: str = "default"
) -> tuple[int, int] | None:
    """Split a pre-addendum `seen.jsonl` into `postings.jsonl` + `alerted/<profile>.jsonl`.

    Returns (postings, alerts) written, or None when there is nothing to migrate. Runs at
    most once: the presence of `postings.jsonl` is the marker, so a second call is a no-op
    even though the legacy file is deliberately left in place.

    Leaving `seen.jsonl` on disk is the point. It is the only copy of several months of
    first-seen dates, and a migration that deletes it before anyone has confirmed the new
    files are good is one bad run away from re-alerting the entire board set.
    """
    legacy = state_dir / LEGACY_PATH.name
    target = postings_path(state_dir)
    if target.exists() or not legacy.exists():
        return None

    entries: dict[str, Entry] = {}
    alerts: dict[str, Alert] = {}
    for lineno, line in enumerate(legacy.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("legacy ledger line %d is unreadable, skipping: %s", lineno, exc)
            continue
        entry: Entry = _from_json(Entry, data)  # type: ignore[assignment]
        if not entry.global_id:
            continue
        entries[entry.global_id] = entry
        if data.get("alerted_at"):
            alerts[entry.global_id] = Alert(
                global_id=entry.global_id,
                alerted_at=str(data["alerted_at"]),
                channel="digest",  # the only channel that existed before the addendum
            )

    save(entries, target)
    _write_jsonl(alerted_path(state_dir, profile), list(alerts.values()))
    log.info(
        "migrated %s: %d postings -> %s, %d alerts -> %s (legacy file left in place)",
        legacy, len(entries), target, len(alerts), alerted_path(state_dir, profile),
    )
    return len(entries), len(alerts)


def next_run_id(entries: dict[str, Entry], health: Health | None = None) -> int:
    """The next run's id. Monotonic across rebuilds.

    Taken from the health file, which is written every run whether or not anything was
    seen. The ledger is only the fallback for an upgrade from a pre-health state.
    """
    from_ledger = max((e.last_seen_run for e in entries.values()), default=0)
    from_health = health.last_run_id if health else 0
    return max(from_ledger, from_health) + 1


# --- cache rebuild ------------------------------------------------------------------

def rebuild(conn: sqlite3.Connection, state: State | dict[str, Entry]) -> int:
    """Seed the derived SQLite cache from the ledger. Returns the posting row count.

    Only the dedup-relevant columns are populated; `description_html` and
    `description_text` are empty until this run's fetch fills them in. `upsert_posting`
    overwrites those on sight, so a posting still live gets its real content back
    immediately and one that has closed keeps a legible-but-bodyless row.

    Alert history is restored into the `alerts` table for every profile in the state, not
    just the one about to run: `jobmon audit` reads across profiles, and a profile skipped
    by `--profile` must not lose its history when the ledger is written back out.
    """
    if isinstance(state, dict):  # a bare entries map, as pre-addendum callers pass
        state = State(entries=state)
    rows = [
        (
            e.global_id, e.source or "ledger", e.company,
            _normalize_company(e.company), e.title, e.normalized_title,
            e.first_location, "[]", None, "", "",
            e.apply_url, None, e.first_seen_at, e.last_seen_at,
            "{}", "US", e.reposted_count, e.last_seen_run,
        )
        for e in state.entries.values()
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO postings (
               global_id, source, company, normalized_company, title, normalized_title,
               first_location, locations_json, remote_type, description_html,
               description_text, apply_url, posted_at, first_seen_at, last_seen_at,
               raw_json, locale, reposted_count, last_seen_run)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    known = set(state.entries)
    conn.executemany(
        """INSERT OR REPLACE INTO alerts (global_id, profile, alerted_at, channel, score)
           VALUES (?,?,?,?,?)""",
        [
            (a.global_id, profile, a.alerted_at, a.channel, a.score)
            for profile, by_id in state.alerts.items()
            for a in by_id.values()
            # The alerts table has a foreign key onto postings. An alert whose posting has
            # fallen out of the ledger would abort the whole rebuild, so drop it here -
            # a posting we no longer track cannot be re-alerted anyway.
            if a.global_id in known
        ],
    )
    # The runs table drives nothing but sequencing; seed it so run ids stay monotonic.
    highest = next_run_id(state.entries) - 1
    if highest > 0:
        conn.execute(
            "INSERT OR REPLACE INTO runs (id, started_at) VALUES (?, ?)",
            (highest, state.entries[next(iter(state.entries))].last_seen_at or ""),
        )
    conn.commit()
    return len(rows)


def _normalize_company(name: str) -> str:
    from .normalize import normalize_company

    return normalize_company(name)


def from_db(conn: sqlite3.Connection) -> dict[str, Entry]:
    """Snapshot the cache's postings back into ledger entries. Called AFTER notification."""
    rows = conn.execute(
        """SELECT global_id, source, company, title, normalized_title, first_location,
                  apply_url, first_seen_at, last_seen_at, last_seen_run, reposted_count
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
            reposted_count=r["reposted_count"] or 0,
        )
        for r in rows
    }


def alerts_from_db(conn: sqlite3.Connection) -> dict[str, dict[str, Alert]]:
    """Snapshot the cache's alert rows back into per-profile ledger files."""
    out: dict[str, dict[str, Alert]] = {}
    for r in conn.execute("SELECT * FROM alerts"):
        out.setdefault(r["profile"], {})[r["global_id"]] = Alert(
            global_id=r["global_id"],
            alerted_at=r["alerted_at"],
            channel=r["channel"],
            score=r["score"],
        )
    return out


def state_from_db(conn: sqlite3.Connection) -> State:
    return State(entries=from_db(conn), alerts=alerts_from_db(conn))


# --- health, for the canary ---------------------------------------------------------

def load_health(path: Path = HEALTH_PATH) -> Health:
    if not path.exists():
        return Health()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Health(
            counts={str(k): int(v) for k, v in (data.get("counts") or {}).items()},
            last_run_id=int(data.get("last_run_id") or 0),
            last_run_at=(str(v) if (v := data.get("last_run_at")) else None),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        log.warning("health file %s unreadable, treating as empty: %s", path, exc)
        return Health()


def save_health(health: Health, path: Path = HEALTH_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "counts": health.counts,
                "last_run_id": health.last_run_id,
                "last_run_at": health.last_run_at,
            },
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


def parse_iso(value: str | None) -> datetime | None:
    """Tolerant ISO parse for ledger timestamps. Returns None on anything unreadable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
