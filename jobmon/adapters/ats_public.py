"""Public ATS board endpoints (spec 5a). PHASE 1 - build this first.

Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee and Personio all expose
public JSON or XML boards readable with no login, no API key, no proxy, and no headless
browser. Read-only public endpoints, so no ToS risk beyond ordinary politeness.

One adapter, per-ATS endpoint templates, company slugs in `companies.yaml`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

name = "ats_public"

# Endpoint templates, formatted with the company slug from companies.yaml.
# The five in scope were swept across all 169 configured boards on 2026-09-06: every one
# returned 200 with a non-empty job list. recruitee and personio are configured but out of
# scope (build step 1) - no slug in companies.yaml uses them, and neither is verified.
#
# Slug case: greenhouse, ashby and smartrecruiters ignore it; lever and workable do NOT
# (jobs.lever.co/nomagic and apply.workable.com/OnLogic-Inc both 404 while the other casing
# returns 200). See CASE_INSENSITIVE_ATS in scripts/bootstrap_companies.py.
#
# smartrecruiters NEVER 404s: an unknown slug returns 200 with totalFound=0, identical to a
# real board with no openings. A typo there is undetectable from the response alone, and the
# zero-result canary (build step 6) cannot distinguish the two cases for this ATS.
ENDPOINTS: dict[str, str] = {
    # Verified: 2026-09-06 - {"jobs": [...]}, description inline in `content`. No N+1.
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    # Verified: 2026-09-06 - top-level LIST, description in `descriptionPlain`. No N+1.
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    # Verified: 2026-09-06 - {"jobs": [...]}, description in `descriptionHtml`. No N+1.
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
    # Verified: 2026-09-06 - {"jobs": [...]}, description inline. id is `shortcode`;
    # `code` exists but is almost always empty (a customer req code, not the id).
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
    # Verified: 2026-09-06 - {"content": [...], "totalFound": n}, page size 100, so this
    # one paginates. List omits the body: from_smartrecruiters still needs a detail fetch.
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
    "recruitee": "https://{slug}.recruitee.com/api/offers/",  # out of scope, unverified
    "personio": "https://{slug}.jobs.personio.de/xml",  # out of scope, unverified; XML
}


PHASE1_ATS = ("greenhouse", "lever", "ashby", "workable")

CACHE_DIR = Path(".cache")

# Politeness, not throughput. 169 boards at 6 concurrent finishes in about ten seconds and
# nobody notices; there is no reason to push harder on someone else's public endpoint.
MAX_CONCURRENCY = 6
# The read timeout is generous because the payloads are big: Anduril's greenhouse board is
# 38.9MB and SpaceX's is 26.5MB with ?content=true. Each takes ~1.3s alone but far longer
# when six downloads share the link. At 30s this tripped on both, and a read timeout drops
# ~4,500 postings while looking exactly like a board that legitimately failed.
TIMEOUT = httpx.Timeout(120.0, connect=10.0)
USER_AGENT = "jobmon/0.1 (personal job search monitor)"


def _list_key(ats: str, payload: Any) -> list[dict[str, Any]]:
    """The job array inside one board's payload. Verified per ATS 2026-09-06."""
    if ats == "lever":
        return payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        return payload.get("jobs") or []
    return []


def fetch(
    client: httpx.Client,
    companies: dict[str, Any] | None = None,
    cache_day: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Sweep every configured (ats, slug) pair and yield raw postings.

    Each yielded dict carries `_ats` and `_company` alongside the untouched payload so
    `normalize.py` knows which parser to apply.

    One slug failing never aborts the sweep: the failure is logged, counted, and the sweep
    continues. A board already fetched in this run is not refetched (spec 5a) - the seen
    set is per-call, and the on-disk cache under `.cache/{day}/` makes a rerun while
    debugging the normalizer hit disk instead of the network.

    smartrecruiters is skipped: its list endpoint omits the body, so it needs pagination
    plus an N+1 detail fetch. Out of scope for Phase 1.
    """
    entries = (companies or {}).get("ats") or []
    day = cache_day or datetime.now(UTC).strftime("%Y-%m-%d")
    seen: set[tuple[str, str]] = set()
    targets = []
    for entry in entries:
        ats, slug = entry["ats"], entry["slug"]
        if ats not in PHASE1_ATS:
            log.debug("skipping %s/%s: not a Phase 1 adapter", ats, slug)
            continue
        if (ats, slug) in seen:
            continue
        seen.add((ats, slug))
        targets.append(entry)

    def one(entry: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
        try:
            return entry, fetch_board(client, entry["ats"], entry["slug"], day=day)
        except Exception as exc:  # noqa: BLE001 - deliberate; see below
            # Spec 5b: each adapter fails loudly and INDEPENDENTLY; one broken board never
            # kills the run. Narrowing this to httpx errors would let a JSON decode error
            # or a schema surprise abort the other 168 boards, which is the failure this
            # catch exists to prevent. It is logged, not swallowed.
            # Type included: httpx.ReadTimeout stringifies to "", so "failed: " alone is
            # an unreadable health line for the failure mode most likely to occur.
            log.warning("board %s/%s failed: %s: %s", entry["ats"], entry["slug"],
                        type(exc).__name__, exc)
            return entry, None

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        for entry, jobs in pool.map(one, targets):
            if jobs is None:
                continue
            for job in jobs:
                yield {**job, "_ats": entry["ats"], "_company": entry["company"]}


def fetch_board(
    client: httpx.Client, ats: str, slug: str, day: str | None = None
) -> list[dict[str, Any]]:
    """Fetch one company's board. Raises on non-200 so the caller can record it.

    A 404 usually means the slug is wrong or the company left the ATS. Note that
    smartrecruiters never 404s - see the ENDPOINTS comment - so this cannot detect a bad
    slug there even when it is in scope.
    """
    day = day or datetime.now(UTC).strftime("%Y-%m-%d")
    cached = cache_path(ats, slug, day)
    if cached.exists():
        return _list_key(ats, json.loads(cached.read_text(encoding="utf-8")))

    response = client.get(ENDPOINTS[ats].format(slug=slug))
    response.raise_for_status()
    payload = response.json()
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(payload), encoding="utf-8")
    return _list_key(ats, payload)


def fetch_detail(client: httpx.Client, ats: str, slug: str, job_id: str) -> dict[str, Any]:
    """Fetch a single posting's full description.

    Only smartrecruiters and recruitee need this, and both are out of scope for Phase 1.
    Greenhouse `?content=true`, Lever, Ashby and Workable all embed the body in the list
    response, which is why those four were chosen: no N+1, no pagination.
    """
    raise NotImplementedError("no Phase 1 adapter needs a detail fetch")


def cache_path(ats: str, slug: str, day: str) -> Path:
    """Where today's raw response for one board lives. `.cache/` is gitignored."""
    return CACHE_DIR / day / f"{ats}-{slug}.json"


def make_client() -> httpx.Client:
    """The one shared client (spec 3): explicit timeouts, a real User-Agent, redirects on."""
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                        headers={"User-Agent": USER_AGENT})
