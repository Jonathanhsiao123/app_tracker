"""One pass of the pipeline (spec 3). PHASE 1.

    [source adapters] -> [normalizer] -> [dedup store] -> [filters] -> [analyzer] -> [notifier]

Single process, run on a schedule. Every 3 hours - faster is not useful, recruiters do not
read a queue in real time.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(slots=True)
class RunResult:
    """What one pass did. Printed by `jobmon run` and folded into the weekly summary."""

    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    alerted: int = 0
    blocked: int = 0
    adapters_failed: int = 0


def run(conn: sqlite3.Connection, dry_run: bool = False) -> RunResult:
    """Execute one full pass.

    Steps:
      1. Load config; open one shared httpx client.
      2. For each enabled adapter: fetch, normalize, upsert. Catch every exception at this
         boundary - **one broken adapter never kills the run** - and record an
         `AdapterHealth` row either way, including on a clean zero-result return.
      3. Analyze every posting that is new or has no analysis row.
      4. Partition into tier-1 instant alerts and the batched digest; send.
      5. `mark_alerted` only after delivery is confirmed.

    `dry_run` does everything except send and except writing `alerted_at`; it prints what
    would have gone out. TODO: this is the flag used while tuning the eligibility regexes,
    so its output needs to show evidence sentences in full.
    """
    raise NotImplementedError


def analyze_pending(conn: sqlite3.Connection) -> int:
    """Analyze postings with no analysis row. Returns the count.
    TODO: `--all` re-analyzes everything after a taxonomy or override edit, without
    re-alerting."""
    raise NotImplementedError


def enabled_adapters() -> list[object]:
    """The adapter modules to sweep this run.

    TODO: a plain list, built by hand. Phase 1 = [ats_public]. Phase 3 adds github_repos
    and whichever `bigtech` modules are in `ENABLED`. No plugin discovery (spec 12).
    """
    raise NotImplementedError
