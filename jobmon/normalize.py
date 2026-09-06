"""Raw source payloads -> canonical `Posting` (spec 4). PHASE 1.

One function per source shape. Everything downstream reads `Posting` only; nothing after
this module is allowed to know what Greenhouse JSON looks like.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import Posting, RemoteType


def normalize(raw: dict[str, Any]) -> Posting | None:
    """Dispatch on `raw["_ats"]` / `raw["_source"]` to the right parser.

    Returns None for a payload that cannot be normalized (missing apply URL, missing
    title), after logging it. TODO: count these per adapter - a normalizer silently
    dropping every posting looks identical to a dead board in the health line.
    """
    raise NotImplementedError


# --- per-source parsers -------------------------------------------------------------
# Each returns a Posting. Keep them dumb and separate; do not factor out a "generic ATS
# parser" until the third one demands it.

def from_greenhouse(raw: dict[str, Any], company: str) -> Posting:
    """TODO: id=`id`, title=`title`, locations=[`location.name`], html=`content`
    (HTML-escaped - unescape before stripping), apply_url=`absolute_url`,
    posted_at=`updated_at`."""
    raise NotImplementedError


def from_lever(raw: dict[str, Any], company: str) -> Posting:
    """TODO: id=`id`, html=`descriptionPlain`/`description` + `lists`, apply_url=`hostedUrl`,
    posted_at=epoch-ms `createdAt`, locations from `categories.location`."""
    raise NotImplementedError


def from_ashby(raw: dict[str, Any], company: str) -> Posting:
    """TODO: id=`id`, html=`descriptionHtml`, apply_url=`jobUrl`, `isRemote` -> remote_type,
    posted_at=`publishedAt`."""
    raise NotImplementedError


def from_workable(raw: dict[str, Any], company: str) -> Posting:
    """TODO: id=`shortcode`, description under `description`+`requirements`, apply_url=`url`."""
    raise NotImplementedError


def from_smartrecruiters(raw: dict[str, Any], company: str) -> Posting:
    """TODO: list endpoint omits the body; the detail fetch supplies `jobAd.sections.*.text`."""
    raise NotImplementedError


def from_recruitee(raw: dict[str, Any], company: str) -> Posting:
    """TODO: id=`id`, html=`description`+`requirements`, apply_url=`careers_url`."""
    raise NotImplementedError


def from_personio(raw: dict[str, Any], company: str) -> Posting:
    """TODO: XML, not JSON. `position` elements; description is CDATA under `jobDescriptions`."""
    raise NotImplementedError


def from_github_repo(raw: dict[str, Any]) -> Posting:
    """A curated-repo table row.

    TODO: these rows have no description at all - only company, role, location, link. Set
    `description_text=""` and accept that eligibility lands on `unknown` and the keyword
    analysis is empty. Optionally follow `apply_url` to the underlying ATS board and reuse
    the matching `from_*` parser to fill the body in; that is the good version, but it is
    an extra request per row, so gate it behind tier-1 companies first.
    """
    raise NotImplementedError


# --- shared helpers -----------------------------------------------------------------

def html_to_text(html: str) -> str:
    """Strip HTML to the plain text every filter and the analyzer read.

    TODO: unescape entities first, drop script/style, convert `<li>` to "- " lines and
    `<br>`/`</p>` to newlines. Sentence boundaries must survive: the eligibility filter is
    sentence-scoped and quotes matched sentences verbatim, so a stripper that collapses
    "...cannot sponsor.Applicants..." into one run will produce garbage evidence.
    """
    raise NotImplementedError


def normalize_title(title: str) -> str:
    """Lowercased, punctuation-stripped title used for the fuzzy dedup key (spec 4).
    TODO: strip req IDs, location suffixes, and cycle years ("2027", "Summer 2027")."""
    raise NotImplementedError


def normalize_company(name_: str) -> str:
    """Canonical company key, shared by dedup and by `sponsorship_overrides.yaml` lookups.
    TODO: casefold, strip legal suffixes (Inc, LLC, Ltd, Corp), collapse punctuation.
    An override keyed "Anduril" must match a posting from "Anduril Industries, Inc."."""
    raise NotImplementedError


def parse_remote_type(text: str, locations: list[str]) -> RemoteType | None:
    """Infer remote/hybrid/onsite. TODO: return None when unstated - do not guess."""
    raise NotImplementedError


def parse_datetime(value: Any) -> datetime | None:
    """Accept ISO 8601, epoch seconds, and epoch milliseconds. TODO: always return
    timezone-aware UTC; naive datetimes silently break the 60-day dedup window."""
    raise NotImplementedError
