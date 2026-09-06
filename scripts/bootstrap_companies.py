"""Extract (company, ats, slug) triples from curated new grad / internship repo READMEs.

Build step 1. Public repo READMEs only - never LinkedIn, Indeed, ZipRecruiter or Glassdoor,
at any point in this project (spec 5).

Scoped to the five ATSes with real US new-grad volume: greenhouse, lever, ashby, workable,
smartrecruiters. `ats_public.ENDPOINTS` also carries recruitee and personio; neither appears
in these lists often enough to pay for a parser, so nothing here emits them.

The two source formats differ and both are handled:
  - SimplifyJobs repos use HTML <tr>/<td> rows (spec 5c calls them "markdown tables"; they
    are not, and have not been for some time).
  - vanshb03 and speedyapply use real markdown pipe rows.

Slug case is preserved as found - SmartRecruiters and Ashby slugs are case-sensitive in the
URL - while dedup is case-insensitive.

Reused by `adapters/github_repos.py` in build step 7b. Keep the parsing functions importable
and free of network calls; only `main` fetches.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Slugs that are really URL path furniture, not a company.
RESERVED_SLUGS = frozenset({"embed", "job_app", "jobs", "j", "api", "search"})

# Ordered: the greenhouse embed form must be tried before the generic greenhouse path, or
# "embed" is read as the slug (T1.4).
ATS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "greenhouse",
        re.compile(
            r"https?://(?:job-)?boards(?:-api)?(?:\.eu)?\.greenhouse\.io/embed/job_app\?"
            r"(?:[^\s\"'<>]*&)?for=([A-Za-z0-9_.-]+)",
            re.IGNORECASE,
        ),
    ),
    (
        "greenhouse",
        re.compile(r"https?://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_.-]+)", re.IGNORECASE),
    ),
    ("lever", re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.IGNORECASE)),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)", re.IGNORECASE)),
    ("workable", re.compile(r"https?://(?:apply|jobs)\.workable\.com/([A-Za-z0-9_.-]+)", re.IGNORECASE)),
    (
        "smartrecruiters",
        re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_.-]+)", re.IGNORECASE),
    ),
)

_HREF = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
_MD_LINK = re.compile(r"\[[^\]]*\]\((https?://[^)\s]+)\)")
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TD = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_HOST = re.compile(r"https?://([^/\s\"'<>]+)", re.IGNORECASE)

# A company cell meaning "same company as the row above".
CONTINUATION = frozenset({"?", "↳", "^", "...", "…"})


@dataclass(slots=True)
class Row:
    """One posting row from a README."""

    company: str
    urls: list[str]
    source: str


@dataclass(slots=True)
class Harvest:
    """What one or more READMEs yielded."""

    entries: list[dict] = field(default_factory=list)  # company / ats / slug / tier
    dropped_hosts: Counter = field(default_factory=Counter)
    dropped_rows: int = 0
    rows_seen: int = 0
    rows_matched: int = 0
    per_source: Counter = field(default_factory=Counter)


def extract_ats(url: str) -> tuple[str, str] | None:
    """(ats, slug) for a supported apply URL, else None.

    Query strings and trailing job paths fall off by construction: the slug pattern stops
    at the first character that cannot appear in a slug (T1.1, T1.2).
    """
    for ats, pattern in ATS_PATTERNS:
        m = pattern.search(url)
        if m:
            slug = m.group(1).strip(".-")
            if slug and slug.lower() not in RESERVED_SLUGS:
                return ats, slug
    return None


def cell_text(cell: str) -> str:
    """Readable text from an HTML or markdown table cell."""
    text = _TAG.sub("", cell)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # markdown link -> its label
    text = text.replace("**", "").replace("</br>", " ")
    return html.unescape(text).strip()


def cell_urls(cell: str) -> list[str]:
    """Every linked URL in a cell. `href` and markdown links only - never `src`, so the
    Apply-button images do not drown the dropped-rows report."""
    return [html.unescape(u) for u in _HREF.findall(cell) + _MD_LINK.findall(cell)]


def _row_from_cells(cells: list[str], last_company: str, source: str) -> tuple[Row, str]:
    company = cell_text(cells[0])
    if company in CONTINUATION or not company:
        company = last_company
    else:
        last_company = company
    urls = [u for cell in cells for u in cell_urls(cell)]
    return Row(company=company, urls=urls, source=source), last_company


def iter_rows(text: str, source: str = "") -> Iterator[Row]:
    """Yield one Row per posting, from HTML <tr> rows or markdown pipe rows.

    A company cell of "?" or a downward arrow inherits the previous row's company, which is
    how both list formats mark additional roles at the same employer.
    """
    last = ""
    html_rows = _TR.findall(text)
    if html_rows:
        for row in html_rows:
            cells = _TD.findall(row)
            if cells:
                built, last = _row_from_cells(cells, last, source)
                yield built
        return

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or all(set(c) <= set("-: ") for c in cells):
            continue  # separator row
        built, last = _row_from_cells(cells, last, source)
        yield built


def harvest(documents: dict[str, str]) -> Harvest:
    """Extract every (company, ats, slug) from `{source_name: readme_text}`.

    Dedups on (ats, lowercased slug); the first source to mention a slug names it (T1.3).
    Rows that carry a link but no supported ATS URL are counted in the dropped report
    (T1.5), by host, so a systematically missed ATS is visible rather than silent.
    """
    out = Harvest()
    seen: set[tuple[str, str]] = set()
    for source, text in documents.items():
        for row in iter_rows(text, source):
            if not row.urls:
                continue  # header or layout row, not a posting
            out.rows_seen += 1
            hits = [h for h in (extract_ats(u) for u in row.urls) if h]
            if not hits:
                out.dropped_rows += 1
                for u in row.urls:
                    m = _HOST.search(u)
                    if m:
                        out.dropped_hosts[m.group(1).lower()] += 1
                continue
            out.rows_matched += 1
            for ats, slug in hits:
                key = (ats, slug.lower())
                if key in seen:
                    continue
                seen.add(key)
                out.entries.append(
                    {"company": row.company or slug, "ats": ats, "slug": slug, "tier": False}
                )
                out.per_source[source] += 1
    out.entries.sort(key=lambda e: (e["ats"], e["slug"].lower()))
    return out


def _q(value: str) -> str:
    """Quote a scalar so `yaml.safe_load` reads it back unchanged."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_yaml(entries: list[dict]) -> str:
    """The `ats:` block for companies.yaml. Hand-rolled so one company stays on one line;
    yaml.dump reflows it into something unreadable in a diff."""
    lines = ["ats:"]
    for e in entries:
        lines.append(
            f"  - {{company: {_q(e['company'])}, ats: {e['ats']}, "
            f"slug: {_q(e['slug'])}, tier: {str(e['tier']).lower()}}}"
        )
    return "\n".join(lines) + "\n"


def report(h: Harvest) -> str:
    lines = [
        f"rows with a link:   {h.rows_seen}",
        f"rows matched:       {h.rows_matched}",
        f"rows dropped:       {h.dropped_rows}",
        f"unique (ats, slug): {len(h.entries)}",
        "",
        "by ats:",
    ]
    for ats, n in Counter(e["ats"] for e in h.entries).most_common():
        lines.append(f"  {ats:<16} {n}")
    lines += ["", "new slugs contributed per source:"]
    for src, n in h.per_source.most_common():
        lines.append(f"  {src:<52} {n}")
    lines += ["", "dropped rows by host (top 15):"]
    for host, n in h.dropped_hosts.most_common(15):
        lines.append(f"  {host:<52} {n}")
    return "\n".join(lines)


def main() -> int:
    import httpx
    import yaml

    ap = argparse.ArgumentParser(description="Bootstrap companies.yaml from curated repos.")
    ap.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "jobmon" / "config" / "companies.yaml",
        help="read github_repos from here (spec 5c)",
    )
    ap.add_argument("--readme", action="append", default=[], help="extra README path or URL")
    ap.add_argument("--out", type=Path, help="write the ats: block here (default: stdout)")
    args = ap.parse_args()

    sources: list[str] = list(args.readme)
    if args.config.exists():
        cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
        sources += [r["url"] for r in (cfg.get("github_repos") or [])]
    if not sources:
        print("no sources: fill github_repos in companies.yaml or pass --readme", file=sys.stderr)
        return 2

    documents: dict[str, str] = {}
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for src in sources:
            if src.startswith("http"):
                r = client.get(src)
                r.raise_for_status()
                documents[src] = r.text
            else:
                documents[src] = Path(src).read_text(encoding="utf-8")

    h = harvest(documents)
    print(report(h), file=sys.stderr)
    block = to_yaml(h.entries)
    if args.out:
        args.out.write_text(block, encoding="utf-8")
    else:
        sys.stdout.write(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
