"""Curated GitHub new grad / internship repos (spec 5c). PHASE 3.

Cheapest high-signal source per line of code, and the *primary* big tech source: the
community updates these within hours, so `bigtech/` only exists for companies these repos
cover poorly. Links are direct-to-ATS, which avoids aggregator ghost listings and reposts.

Repo names roll over by cycle year, so URLs come from `companies.yaml`, never hardcoded.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx

name = "github_repo"


def fetch(client: httpx.Client, repo_urls: list[str] | None = None) -> Iterator[dict[str, Any]]:
    """Fetch each configured repo's README and yield one dict per table row.

    TODO: fetch the raw markdown (`raw.githubusercontent.com/.../README.md`), not the
    rendered HTML page. Send `If-None-Match` with the stored ETag and skip parsing on 304 -
    these READMEs are large and mostly unchanged between 3-hour runs.
    """
    raise NotImplementedError


def parse_markdown_table(markdown: str, repo_url: str) -> list[dict[str, Any]]:
    """Parse the posting table out of a curated README.

    Columns are conventionally Company / Role / Location / Application link / Date posted,
    but the exact set and order differ per repo and change mid-cycle.

    TODO:
      - Map columns by *header text*, not index. Fail loudly if a required header is
        missing - a silently reordered table is worse than a crash.
      - Handle the leading-arrow continuation rows (`↳`) that mean "same company as above".
      - Strip the emoji status markers (closed / no-sponsorship / simplify-redirect links)
        some repos use, and keep them as flags rather than discarding.
      - Unwrap the link-shortener/redirect wrappers to recover the real ATS URL.
    """
    raise NotImplementedError


def is_closed_row(row: dict[str, Any]) -> bool:
    """Whether a row is marked closed/expired by the repo's own convention. TODO: closed
    rows are still stored (nothing is ever silently discarded) but are not alerted."""
    raise NotImplementedError
