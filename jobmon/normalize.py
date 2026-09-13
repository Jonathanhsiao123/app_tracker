"""Raw source payloads -> canonical `Posting` (spec 4). PHASE 1.

One function per source shape. Everything downstream reads `Posting` only; nothing after
this module is allowed to know what Greenhouse JSON looks like.

Payload shapes verified against live boards 2026-09-06; see `adapters/ats_public.ENDPOINTS`
for the per-ATS notes. SmartRecruiters is out of scope for Phase 1: its list endpoint omits
the description body, so it is the only ATS needing both pagination and an N+1 detail fetch.
"""

from __future__ import annotations

import hashlib
import html as _html
import logging
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from .models import Posting, RemoteType

log = logging.getLogger(__name__)

# Parsers wired up so far. smartrecruiters/recruitee/personio are deliberately absent.
PARSERS = ("greenhouse", "lever", "ashby", "workable", "github_repo")


def normalize(raw: dict[str, Any]) -> Posting | None:
    """Dispatch on `raw["_ats"]` / `raw["_source"]` to the right parser.

    Returns None for a payload that cannot be normalized (missing apply URL, missing
    title), after logging it. The runner counts these per adapter: a normalizer silently
    dropping every posting looks identical to a dead board in the health line.
    """
    ats = raw.get("_ats") or raw.get("_source")
    company = raw.get("_company") or ""
    parser = {
        "greenhouse": from_greenhouse,
        "lever": from_lever,
        "ashby": from_ashby,
        "workable": from_workable,
        "github_repo": from_github_repo,
    }.get(ats)
    if parser is None:
        log.warning("no parser for source %r; skipping", ats)
        return None
    try:
        return parser(raw, company)
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("normalize failed for %s/%s: %s", ats, company, exc)
        return None


# --- per-source parsers -------------------------------------------------------------
# Each returns a Posting. Keep them dumb and separate; do not factor out a "generic ATS
# parser" until the third one demands it.

def from_greenhouse(raw: dict[str, Any], company: str) -> Posting:
    """Verified 2026-09-06: id=`id`, html=`content` (HTML-ESCAPED, unescape first),
    apply_url=`absolute_url`, locations=[`location.name`], posted_at=`updated_at`."""
    job_id = str(_require(raw, "id"))
    title = _require(raw, "title")
    body_html = _html.unescape(raw.get("content") or "")
    location = (raw.get("location") or {}).get("name")
    return _build(
        source="greenhouse",
        company=company or raw.get("company_name") or "",
        job_id=job_id,
        title=title,
        locations=[location] if location else [],
        description_html=body_html,
        apply_url=_require(raw, "absolute_url"),
        posted_at=parse_datetime(raw.get("updated_at") or raw.get("first_published")),
        raw=raw,
    )


def from_lever(raw: dict[str, Any], company: str) -> Posting:
    """Verified 2026-09-06: id=`id`, apply_url=`hostedUrl`, posted_at=epoch-ms `createdAt`,
    locations from `categories.location`. Body is `description` plus the `lists` blocks,
    which is where the requirements (and the sponsorship language) actually live."""
    cats = raw.get("categories") or {}
    parts = [raw.get("description") or ""]
    for block in raw.get("lists") or []:
        parts.append(f"<h3>{block.get('text', '')}</h3>{block.get('content', '')}")
    parts.append(raw.get("additional") or "")
    locations = [loc for loc in (cats.get("allLocations") or [cats.get("location")]) if loc]
    return _build(
        source="lever",
        company=company,
        job_id=str(_require(raw, "id")),
        title=_require(raw, "text"),
        locations=locations,
        description_html="".join(parts),
        apply_url=_require(raw, "hostedUrl"),
        posted_at=parse_datetime(raw.get("createdAt")),
        raw=raw,
    )


def from_ashby(raw: dict[str, Any], company: str) -> Posting:
    """Verified 2026-09-06: id=`id`, html=`descriptionHtml`, apply_url=`jobUrl`,
    `isRemote` -> remote_type, posted_at=`publishedAt`."""
    locations = [loc for loc in [raw.get("location")] if loc]
    locations += [
        s.get("location") for s in (raw.get("secondaryLocations") or []) if s.get("location")
    ]
    return _build(
        source="ashby",
        company=company,
        job_id=str(_require(raw, "id")),
        title=_require(raw, "title"),
        locations=locations,
        description_html=raw.get("descriptionHtml") or "",
        apply_url=raw.get("jobUrl") or _require(raw, "applyUrl"),
        posted_at=parse_datetime(raw.get("publishedAt")),
        remote_type="remote" if raw.get("isRemote") else None,
        raw=raw,
    )


def from_workable(raw: dict[str, Any], company: str) -> Posting:
    """Verified against live payloads 2026-09-06: id=`shortcode`, apply_url=
    `application_url` (falling back to `url`), body is `description` plus `requirements`,
    locations from `city`/`country`, posted_at=`created_at`.

    `code` also exists on every job and is almost always the EMPTY STRING - it is a
    customer-defined req code, not the id. Reading the key list without reading the values
    makes `code` look like the identifier; it is not.
    """
    where = ", ".join(p for p in [raw.get("city"), raw.get("country")] if p)
    return _build(
        source="workable",
        company=company,
        job_id=str(_require(raw, "shortcode")),
        title=_require(raw, "title"),
        locations=[where] if where else [],
        description_html=(raw.get("description") or "") + (raw.get("requirements") or ""),
        apply_url=raw.get("application_url") or _require(raw, "url"),
        posted_at=parse_datetime(raw.get("created_at") or raw.get("published_on")),
        raw=raw,
    )


def from_smartrecruiters(raw: dict[str, Any], company: str) -> Posting:
    """Out of scope for Phase 1. The list endpoint omits the body, so this needs the
    detail fetch (`jobAd.sections.*.text`) plus offset/limit pagination - the only ATS in
    the set requiring either. 48% of postings, 13 of 169 boards, mostly non-SWE."""
    raise NotImplementedError("smartrecruiters is out of scope for Phase 1")


def from_recruitee(raw: dict[str, Any], company: str) -> Posting:
    """Out of scope: no configured slug uses it, and the endpoint is unverified."""
    raise NotImplementedError("recruitee is out of scope")


def from_personio(raw: dict[str, Any], company: str) -> Posting:
    """Out of scope: XML, and no configured slug uses it."""
    raise NotImplementedError("personio is out of scope")


def from_github_repo(raw: dict[str, Any], company: str) -> Posting:
    """A curated-repo table row (spec 5c, build step 7b).

    These rows carry no description body at all - only company, role, location, an apply
    link, and a loosely-formatted date. `description_text` is therefore empty, which means
    eligibility lands on `unknown` (never `blocked` - there is no text to match against) and
    the keyword analyzer reports nothing. This is the accepted trade-off of this source
    (spec 5c): it is cheap and direct-to-ATS, not a full posting body. When the same job also
    exists in `companies.yaml`'s ATS sweep, that Posting carries the real body and the
    cross-source fuzzy dedup (spec 4) means only one of the two ever reaches an alert.

    `job_id` has no natural identifier here - unlike Greenhouse/Lever/Ashby/Workable, a
    table row is not issued one - so it is a hash of `apply_url`, the one field that stays
    stable for a given row run over run (repos edit titles and reorder rows far more often
    than a posting's own apply link changes).
    """
    apply_url = _require(raw, "apply_url")
    title = _require(raw, "title")
    job_id = hashlib.sha256(apply_url.encode()).hexdigest()[:16]
    return _build(
        source="github_repo",
        company=company,
        job_id=job_id,
        title=title,
        locations=raw.get("locations") or [],
        description_html="",
        apply_url=apply_url,
        posted_at=parse_repo_date(raw.get("posted_text")),
        raw=raw,
    )


# --- construction -------------------------------------------------------------------

def _require(raw: dict[str, Any], key: str) -> Any:
    """Fail at the adapter, not at notify (T3c). A payload missing a required field is an
    adapter bug or an API change, and both should be loud."""
    value = raw.get(key)
    if value in (None, ""):
        raise KeyError(f"required field {key!r} missing from payload")
    return value


def _build(
    *,
    source: str,
    company: str,
    job_id: str,
    title: str,
    locations: list[str],
    description_html: str,
    apply_url: str,
    posted_at: datetime | None,
    raw: dict[str, Any],
    remote_type: RemoteType | None = None,
) -> Posting:
    now = datetime.now(UTC)
    text = html_to_text(description_html)
    return Posting(
        global_id=Posting.make_global_id(source, company, job_id),
        source=source,
        company=company,
        title=title,
        locations=locations,
        description_html=description_html,
        description_text=text,
        apply_url=apply_url,
        first_seen_at=now,
        last_seen_at=now,
        remote_type=remote_type or parse_remote_type(text, locations),
        posted_at=posted_at,
        raw=raw,  # untouched, whatever nested junk it carries (T3c)
    )


# --- shared helpers -----------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}
_DROP_TAGS = {"script", "style", "head"}


class _TextExtractor(HTMLParser):
    """HTML -> text, preserving the line and sentence boundaries the eligibility filter
    depends on. A stripper that runs "...cannot sponsor.Applicants..." together produces
    garbage evidence, which is the one output the user reads verbatim."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _DROP_TAGS:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS or tag == "li":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Strip HTML to the plain text every filter and the analyzer read."""
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(_html.unescape(html) if "&lt;" in html else html)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


_REQ_ID = re.compile(r"\b(?:req|jr|r)[-_ ]?\d{3,}\b", re.IGNORECASE)
_CYCLE = re.compile(r"\b(?:summer|fall|winter|spring)?\s*20\d{2}\b", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]+")
_LEGAL = re.compile(
    r"\b(?:inc|llc|ltd|limited|corp|corporation|co|plc|gmbh|holdings?|group|技術)\b\.?",
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    """Lowercased, punctuation-stripped title used for the fuzzy dedup key (spec 4).
    Req IDs and cycle years are stripped so a repost with a new req number still matches."""
    t = _REQ_ID.sub(" ", title or "")
    t = _CYCLE.sub(" ", t)
    t = _PUNCT.sub(" ", t).lower()
    return " ".join(t.split())


def normalize_company(name_: str) -> str:
    """Canonical company key, shared by dedup and by `sponsorship_overrides.yaml` lookups.
    An override keyed "Anduril" must match a posting from "Anduril Industries, Inc."."""
    n = _PUNCT.sub(" ", name_ or "").lower()
    n = _LEGAL.sub(" ", n)
    return " ".join(n.split())


_REMOTE = re.compile(r"\b(?:fully|100%)?\s*remote\b", re.IGNORECASE)
_HYBRID = re.compile(r"\bhybrid\b", re.IGNORECASE)
_ONSITE = re.compile(r"\b(?:on-?site|in-?office|in-?person)\b", re.IGNORECASE)


def parse_remote_type(text: str, locations: list[str]) -> RemoteType | None:
    """Infer remote/hybrid/onsite from an explicit statement only. Returns None when
    unstated - the spec says do not guess, and "Remote" inside "Remote team culture" is
    exactly the kind of guess that produces a wrong label in every alert."""
    haystack = " ".join(locations or []) + " " + (text or "")[:400]
    if _HYBRID.search(haystack):
        return "hybrid"
    if _REMOTE.search(haystack):
        return "remote"
    if _ONSITE.search(haystack):
        return "onsite"
    return None


def parse_repo_date(text: str | None) -> datetime | None:
    """Best-effort parse of a curated repo's date column: either a relative day count
    ("2d", speedyapply's Age) or a bare "Mon DD" with no year (vanshb03's Date Posted).
    Returns None rather than guessing when the format is unrecognized - `first_seen_at` is
    what actually drives alert timing regardless of whether this parses.

    A "Mon DD" with no year is inherently ambiguous near a year boundary: if reading it
    against the current year lands in the future, it must mean last year instead.
    """
    if not text:
        return None
    text = text.strip()
    m = _AGE_DAYS.match(text)
    if m:
        return datetime.now(UTC) - timedelta(days=int(m.group(1)))
    try:
        parsed = datetime.strptime(text, "%b %d").replace(year=datetime.now(UTC).year, tzinfo=UTC)
    except ValueError:
        return None
    if parsed > datetime.now(UTC) + timedelta(days=1):
        parsed = parsed.replace(year=parsed.year - 1)
    return parsed


_AGE_DAYS = re.compile(r"^(\d+)\s*d$", re.IGNORECASE)


def parse_datetime(value: Any) -> datetime | None:
    """Accept ISO 8601, epoch seconds, and epoch milliseconds. Always returns
    timezone-aware UTC; a naive datetime silently breaks the 60-day dedup window."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        n = float(value)
        if n > 1e11:  # milliseconds
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value))  # 3.11+ accepts a trailing Z
    except ValueError:
        log.debug("unparseable datetime %r", value)
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
