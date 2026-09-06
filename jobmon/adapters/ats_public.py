"""Public ATS board endpoints (spec 5a). PHASE 1 - build this first.

Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee and Personio all expose
public JSON or XML boards readable with no login, no API key, no proxy, and no headless
browser. Read-only public endpoints, so no ToS risk beyond ordinary politeness.

One adapter, per-ATS endpoint templates, company slugs in `companies.yaml`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import httpx

name = "ats_public"

# Endpoint templates, formatted with the company slug from companies.yaml.
# TODO: verify each against a live board before Phase 1 ships; several of these APIs have
# both a list form and a `?content=true` form that embeds the description. Prefer the form
# that avoids an N+1 detail fetch per posting.
ENDPOINTS: dict[str, str] = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
    "recruitee": "https://{slug}.recruitee.com/api/offers/",
    "personio": "https://{slug}.jobs.personio.de/xml",  # XML, not JSON
}


def fetch(client: httpx.Client, companies: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Sweep every configured (ats, slug) pair and yield raw postings.

    Each yielded dict carries `_ats` and `_company` alongside the untouched payload so
    `normalize.py` knows which parser to apply.

    TODO:
      - Skip a board already fetched in this run (spec 5a: never refetch twice per run).
      - Cache raw responses for the day under `.cache/{date}/{ats}-{slug}.json`; a rerun
        while debugging the normalizer should hit disk, not the network.
      - One slug failing must not abort the sweep: log it, count it, keep going.
      - SmartRecruiters paginates (`offset`/`limit`); Greenhouse and Lever do not.
    """
    raise NotImplementedError


def fetch_board(client: httpx.Client, ats: str, slug: str) -> list[dict[str, Any]]:
    """Fetch one company's board from one ATS. TODO: raise on non-200 so the caller can
    record it; a 404 usually means the slug is wrong or the company left the ATS."""
    raise NotImplementedError


def fetch_detail(client: httpx.Client, ats: str, slug: str, job_id: str) -> dict[str, Any]:
    """Fetch a single posting's full description.

    Only needed for boards whose list endpoint omits the body. TODO: SmartRecruiters and
    Recruitee need this; Greenhouse `?content=true` and Lever do not. Keep the per-posting
    call count visible in the health line so this does not quietly become the slow path.
    """
    raise NotImplementedError


def cache_path(ats: str, slug: str, day: str) -> Path:
    """Where today's raw response for one board lives. TODO: `.cache/` is gitignored."""
    raise NotImplementedError
