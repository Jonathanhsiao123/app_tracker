"""Curated GitHub new grad / internship repos (spec 5c). PHASE 3.

Cheapest high-signal source per line of code, and the *primary* big tech source: the
community updates these within hours, so `bigtech/` only exists for companies these repos
cover poorly. Links are direct-to-ATS, which avoids aggregator ghost listings and reposts.

Repo names roll over by cycle year, so URLs come from `companies.yaml`, never hardcoded.

Verified against the three live repos configured in `companies.yaml` on 2026-09-12:
  - SimplifyJobs/Summer2027-Internships: HTML `<table>`, one per job category (SWE, PM,
    Data Science, Quant, Hardware), each with its own repeated header row. Application cell
    holds TWO links: the real apply URL first, then a `simplify.jobs/p/...` companion link.
    Closed postings are removed from the table entirely and moved to a separate
    README-Inactive.md - not configured as a source, so there is nothing to mark closed in
    practice; `is_closed_row` is a safety net, not something currently exercised.
  - vanshb03/New-Grad-2027, speedyapply/2027-SWE-College-Jobs: real markdown pipe rows, one
    apply link per row. speedyapply calls the link column "Posting" and inserts a "Salary"
    column between Location and it; vanshb03 and Simplify call it "Application"/"Application/
    Link". Column POSITION differs across all three - only the header text is stable, which
    is why columns are mapped by header, never by index (spec 7b).
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

name = "github_repo"

# `scripts/` is not an installed package (see pyproject.toml); reached the same way
# tests/test_bootstrap_companies.py does, so the parsing helpers below - cell text/url
# extraction, the HTML-or-markdown row splitter, the "?"/"↳" continuation convention - are
# written exactly once (build step 7b: "reuse parsing from bootstrap_companies, do not
# write it twice").
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import bootstrap_companies as bc  # path must be set up (above) before this import resolves

CACHE_DIR = Path(".cache")

# Header cell text (post `cell_text`, lowercased) -> canonical field. A repo's header row
# repeats before every sub-table (Simplify has one per job category), so header detection
# runs on every row, not just the document's first - a later sub-table with a differently
# ordered or differently worded header still maps correctly.
_HEADER_FIELDS = {
    "company": "company",
    "role": "role", "position": "role", "title": "role",
    "location": "location",
    "application/link": "link", "application": "link", "link": "link", "posting": "link",
    "date posted": "date", "date": "date", "age": "date", "posted": "date",
}
# Used only if a repo's table has no recognizable header at all - a fallback for a future
# repo, not exercised by the three currently configured (all have real header rows).
_DEFAULT_ORDER = ("company", "role", "location", "link", "date")

_CLOSED = re.compile(r"\U0001f512|\bclosed\b|\bexpired\b", re.IGNORECASE)
_AGE_DAYS = re.compile(r"^(\d+)\s*d$", re.IGNORECASE)

# Multi-location cells (vanshb03: "Chicago, IL</br>New York, NY") join two locations with a
# raw `<br>`-family tag. `bc.cell_text` cannot be used to find this boundary: its generic
# `<[^>]+>` tag-stripper runs BEFORE its own `</br>` -> space replacement, so `<br>` is
# already gone by the time that replacement would fire, and the two locations concatenate
# with no separator at all ("Chicago, ILNew York, NY"). Split on the tag first, here, then
# clean each fragment.
_BR_SPLIT = re.compile(r"</?br\s*/?>", re.IGNORECASE)
# Long lists are collapsed as "<details><summary>4 locations</summary>Palo Alto, CA</br>...",
# and the summary label used to glue onto the first city: "4 locationsPalo Alto, CA".
_SUMMARY = re.compile(r"<summary>.*?</summary>", re.IGNORECASE | re.DOTALL)


def _split_locations(raw_cell: str) -> list[str]:
    cell = _SUMMARY.sub("", raw_cell)
    return [t for part in _BR_SPLIT.split(cell) if (t := bc.cell_text(part))]


def fetch(client: httpx.Client, companies: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Fetch each repo configured under `companies.yaml`'s `github_repos:` and yield one raw
    dict per posting row.

    One repo failing to fetch or parse never aborts the others (spec 5b): logged and
    skipped, like every other adapter boundary in this project.
    """
    repos = (companies or {}).get("github_repos") or []
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    for repo in repos:
        url = repo["url"]
        kind = repo.get("kind", "both")
        try:
            text = _fetch_readme(client, url, day)
        except Exception as exc:  # noqa: BLE001 - one repo's outage must not sink the rest
            log.warning("github repo %s failed to fetch: %s: %s", url, type(exc).__name__, exc)
            continue
        try:
            rows = parse_markdown_table(text, url)
        except Exception as exc:  # noqa: BLE001 - a parser surprise is data, not a crash
            log.warning("github repo %s failed to parse: %s: %s", url, type(exc).__name__, exc)
            continue
        for row in rows:
            yield {
                **row,
                "_ats": "github_repo",
                "_company": row["company"],
                "closed": is_closed_row(row),
                "repo_kind": kind,
                "source_repo": url,
            }


def _fetch_readme(client: httpx.Client, url: str, day: str) -> str:
    """One repo's raw markdown, cached per day like `ats_public` (spec 3: no source fetched
    twice in one run). These READMEs run hundreds of KB, so a rerun while debugging the
    parser hits disk instead of the network."""
    cached = cache_path(url, day)
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    response = client.get(url)
    response.raise_for_status()
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(response.text, encoding="utf-8")
    return response.text


def cache_path(url: str, day: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9]+", "-", url).strip("-")[-80:]
    return CACHE_DIR / day / f"github-{safe}.md"


def parse_markdown_table(markdown: str, repo_url: str) -> list[dict[str, Any]]:
    """Every posting row out of a curated README, across every sub-table in the document.

    Columns are mapped by *header text*, not index (spec 7b): a repo reorders or adds a
    column (speedyapply inserts "Salary" between Location and the link column) often enough
    that a positional read would silently misassign fields. The header row itself is
    identified by its first cell reading "company" - reliable because a real company is
    never named that, and because it recurs before every sub-table, so multi-table documents
    (Simplify) re-map correctly at each boundary instead of using the first table's layout
    for every later one.

    Rows with no company at all (banner/summary rows) or no link in the mapped link column
    (closed-section links, category headers) are not postings and are dropped, not stored -
    that is a document-structure question, not the closed/open OF-a-posting question that
    `is_closed_row` answers.
    """
    rows: list[dict[str, Any]] = []
    field_index: dict[str, int] | None = None
    last_company = ""
    saw_header = False
    for raw_cells in bc.iter_cell_rows(markdown):
        cleaned = [bc.cell_text(c) for c in raw_cells]
        if cleaned and cleaned[0].strip().lower() == "company":
            field_index = _map_header(cleaned)
            saw_header = True
            continue
        if field_index is None:
            if len(cleaned) < len(_DEFAULT_ORDER):
                continue  # not a full row yet and no header seen; skip rather than guess
            field_index = dict(zip(_DEFAULT_ORDER, range(len(_DEFAULT_ORDER))))
        row = _row_from_fields(cleaned, raw_cells, field_index, last_company)
        if row is None:
            continue
        last_company = row["company"]
        rows.append(row)
    if not saw_header:
        log.warning("github repo %s: no header row found; used positional fallback", repo_url)
    return rows


def _map_header(cleaned_header: list[str]) -> dict[str, int]:
    index: dict[str, int] = {}
    for i, cell in enumerate(cleaned_header):
        field = _HEADER_FIELDS.get(cell.strip().lower())
        if field:
            index[field] = i  # last occurrence wins: this sub-table's own layout
    return index


def _row_from_fields(
    cleaned: list[str],
    raw_cells: list[str],
    field_index: dict[str, int],
    last_company: str,
) -> dict[str, Any] | None:
    def cell(field: str) -> str:
        i = field_index.get(field)
        return cleaned[i] if i is not None and i < len(cleaned) else ""

    company = cell("company")
    if company in bc.CONTINUATION or not company:
        company = last_company
    if not company:
        return None  # header/banner row, not a posting - no company at all, even inherited

    link_i = field_index.get("link")
    urls = bc.cell_urls(raw_cells[link_i]) if link_i is not None and link_i < len(raw_cells) else []
    if not urls:
        return None  # no apply link - a summary or section-header row, not a posting

    # The real apply URL, not the `simplify.jobs/p/...` companion link some rows carry
    # alongside it (see module docstring). When every link in the cell is a simplify.jobs
    # one, there is nothing better to pick; that shortlink is the best available URL.
    apply_url = next((u for u in urls if "simplify.jobs" not in u.lower()), urls[0])

    loc_i = field_index.get("location")
    location_cell = raw_cells[loc_i] if loc_i is not None and loc_i < len(raw_cells) else ""

    return {
        "company": company,
        "title": cell("role"),
        "locations": _split_locations(location_cell),
        "apply_url": apply_url,
        "posted_text": cell("date") or None,
    }


def is_closed_row(row: dict[str, Any]) -> bool:
    """Whether a row is marked closed/expired by the repo's own convention.

    None of the three configured repos currently mark closed rows inline - see the module
    docstring; Simplify removes them from the table rather than flagging them in place. This
    is a safety net for a convention change or a future repo that does mark rows inline. A
    row this catches is still yielded and stored by `fetch` (nothing is ever silently
    discarded, spec 6/9's rule applied here too) - it is `notify.should_alert` that reads the
    `closed` flag off `Posting.raw` and suppresses the alert, the same shape as `blocked`.
    """
    haystack = f"{row.get('company', '')} {row.get('title', '')}"
    return bool(_CLOSED.search(haystack))
