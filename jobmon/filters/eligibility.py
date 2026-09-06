"""International-student eligibility classification (spec 6). PHASE 2.

The most valuable filter in the system: it removes postings that are unwinnable regardless
of fit. Also the easiest one to get dangerously wrong, so:

    THE FILTER SORTS AND LABELS. IT DOES NOT DECIDE.

Three-way, never a silent discard:
    blocked   - strong textual evidence. Suppressed from the alert, still stored,
                visible via `--show-blocked`.
    likely_ok - explicit sponsorship / work-authorization-friendly language. Alerted,
                flagged positive.
    unknown   - no relevant language. THE MAJORITY CASE. Default. Alerted normally.
                `unknown` must never be treated as `blocked`.

`eligibility_evidence` always carries the matched sentence VERBATIM, so a bad classification
takes two seconds to overrule by eye.
"""

from __future__ import annotations

from typing import Any

from ..models import Eligibility

# --- blocking signals (regex over description_text, SENTENCE-SCOPED) ----------------
# TODO: compile with re.IGNORECASE and word boundaries. Scope every match to its sentence
# so the evidence string is quotable.

CITIZENSHIP_PATTERNS = [
    r"must be a u\.?s\.? citizen",
    r"u\.?s\.? citizenship (?:is )?required",
    r"\bus person\b",  # as defined by ITAR/EAR
]

CLEARANCE_PATTERNS = [
    r"active security clearance",
    r"\bTS/SCI\b",
    r"secret clearance",
    r"ability to obtain a (?:security )?clearance",
]

EXPORT_CONTROL_PATTERNS = [
    r"\bITAR\b",
    r"\bEAR\b",
    r"export control(?:led|s)? regulations?",
]

SPONSORSHIP_REFUSAL_PATTERNS = [
    r"unable to sponsor",
    r"will not sponsor",
    r"no visa sponsorship",
    r"does not (?:provide|offer) sponsorship",
    r"permanent work authorization without the need for sponsorship",
    r"not eligible for (?:CPT|OPT)",
]

# --- positive signals ---------------------------------------------------------------

POSITIVE_PATTERNS = [
    r"visa sponsorship available",
    r"will sponsor",
    r"\bH-?1B\b",
    r"we sponsor",
    r"CPT and OPT eligible",
    r"F-?1 students welcome",
]

# --- the trap, stated explicitly (spec 6) -------------------------------------------
# "must be authorized to work in the US" is NOT a block: F-1 with CPT/OPT *is* authorized.
# Keep "authorized to work" out of the blocking list entirely. A clearance requirement, by
# contrast, is a genuine hard block. Do not let one regex conflate the two.
NEVER_BLOCK_ON = [
    r"authorized to work",
    r"legally authorized",
    r"work authorization",
]


def classify(
    description_text: str, company: str, overrides: dict[str, dict[str, Any]] | None = None
) -> tuple[Eligibility, list[str]]:
    """Return (label, evidence_sentences).

    Order of resolution:
      1. Company-level override, if present. **Overrides beat text classification.**
      2. Blocking signals -> "blocked".
      3. Positive signals -> "likely_ok".
      4. Otherwise "unknown".

    TODO: when both a blocking and a positive signal match, return "blocked" and include
    BOTH sentences as evidence - that combination is usually a multi-req posting or
    boilerplate collision, and it is exactly the case worth eyeballing.
    """
    raise NotImplementedError


def find_sentences(description_text: str, patterns: list[str]) -> list[str]:
    """Return the sentences containing any pattern match, verbatim and untrimmed.

    Verbatim is the whole point: the evidence is what makes a wrong label cost two seconds
    instead of a lost opportunity. TODO: split on sentence boundaries that survive the HTML
    stripper - bullet lines have no terminal period, so treat a newline as a boundary too.
    """
    raise NotImplementedError


def apply_override(company: str, overrides: dict[str, dict[str, Any]]) -> tuple[Eligibility, str] | None:
    """Look up `sponsorship_overrides.yaml` (spec 6). Returns (label, note) or None.

    TODO: key on `normalize.normalize_company` so "Anduril Industries, Inc." matches the
    "anduril" entry. Include the override's `note` and `checked_on` date in the evidence
    list, so a stale hand-check is visible in the alert rather than invisible.
    """
    raise NotImplementedError


# --- known limitations, kept in the code where they will actually be read ------------
#
# * Posting text is a weak proxy. Companies that sponsor routinely often say nothing, and
#   boilerplate refusal language sometimes sits on reqs the recruiter will happily override.
# * Seed the overrides file from public H-1B/PERM disclosure data (DOL LCA filings and the
#   aggregators built on them) plus known defense and government-contractor employers. Once,
#   for the top ~60 targets. One evening, and more reliable than any text heuristic.
# * A regex will occasionally block something winnable. Nothing is deleted, evidence is
#   always shown, `--show-blocked` exists.
