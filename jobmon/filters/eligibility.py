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

Case sensitivity is deliberate and mixed. Acronyms are matched case-SENSITIVELY via inline
`(?-i:...)`: `EAR` under IGNORECASE matches the ordinary word "ear", and `OPT` matches
"opt". Everything else is case-insensitive.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import Eligibility
from ..normalize import normalize_company

# --- blocking signals (regex over description_text, SENTENCE-SCOPED) ----------------

CITIZENSHIP_PATTERNS = [
    r"must be a u\.?s\.?\s*(?:citizen|national)",
    r"u\.?s\.?\s*citizenship (?:is )?required",
    r"(?-i:\bU\.?S\.?\s+Person)",  # as defined by ITAR/EAR
    r"restricted to u\.?s\.? citizens",
    r"citizenship requirement",
]

CLEARANCE_PATTERNS = [
    r"active (?:security |government )?clearance",
    r"(?-i:\bTS/SCI\b)",
    r"(?:top ?secret|secret|confidential) clearance",
    r"ability to obtain (?:and maintain )?a (?:security )?clearance",
    r"security clearance (?:is )?required",
    r"(?-i:\bpoly(?:graph)?\b)\s*(?:required|clearance)",
]

# Person-status language. Export control only blocks when it appears ALONGSIDE one of
# these in the same sentence - see below for why.
_PERSON_STATUS = (
    r"(?:u\.?s\.?\s+persons?|u\.?s\.?\s+citizens?|\bcitizens?\b|permanent residents?"
    r"|green card|lawful permanent)"
)

# Measured against 13,608 real postings on 2026-09-06: a bare `\bITAR\b` matched 6,170
# sentences, and the overwhelming majority were SKILLS, not gates -
#   "Familiarity with export control regulations (ITAR/EAR) relevant to robotics hardware"
#   "Ensure purchasing activities are compliant with external regulations (e.g., ITAR, FAR)"
# Blocking on those hides jobs you could actually take. The genuine gate is the person
# requirement, and real postings state it: "Must be a U.S. Person in accordance with ITAR".
#
# So export control blocks only with person-status language in the same sentence, or when
# the POSITION itself is declared subject to the regime. A posting that mentions ITAR and
# nothing else now lands on `unknown` and still alerts. That direction is deliberate: a
# false `unknown` costs you one alert to read, a false `blocked` costs you the job.
EXPORT_CONTROL_PATTERNS = [
    rf"(?-i:\bITAR\b)[^.\n]{{0,100}}?{_PERSON_STATUS}",
    rf"{_PERSON_STATUS}[^.\n]{{0,100}}?(?-i:\bITAR\b)",
    rf"(?-i:\bEAR\b)[^.\n]{{0,100}}?{_PERSON_STATUS}",
    rf"{_PERSON_STATUS}[^.\n]{{0,100}}?(?-i:\bEAR\b)",
    (
        r"(?:subject to|governed by|restricted by)[^.\n]{0,40}?"
        r"(?:export control|(?-i:\bITAR\b)|(?-i:\bEAR\b))"
    ),
    (
        r"export control(?:led|s)? (?:regulations?|laws?)[^.\n]{0,60}?"
        rf"{_PERSON_STATUS}"
    ),
]

SPONSORSHIP_REFUSAL_PATTERNS = [
    r"un(?:able|willing) to sponsor",
    r"(?:will|can|do(?:es)?) not (?:be able to )?sponsor",
    r"no (?:visa )?sponsorship",
    r"does not (?:provide|offer|support) (?:visa )?sponsorship",
    r"sponsorship (?:is )?not (?:available|offered|provided)",
    r"without the need for (?:current or future )?sponsorship",
    r"not eligible for (?:CPT|OPT)",
    r"cannot (?:provide|offer) (?:visa )?sponsorship",
]

BLOCKING_GROUPS: dict[str, list[str]] = {
    "citizenship": CITIZENSHIP_PATTERNS,
    "clearance": CLEARANCE_PATTERNS,
    "export_control": EXPORT_CONTROL_PATTERNS,
    "sponsorship_refusal": SPONSORSHIP_REFUSAL_PATTERNS,
}
BLOCKING_PATTERNS = [p for group in BLOCKING_GROUPS.values() for p in group]

# --- positive signals ---------------------------------------------------------------

POSITIVE_PATTERNS = [
    r"(?:visa )?sponsorship (?:is |are )?(?:available|provided|offered)",
    r"will(?: be able to)? sponsor",
    r"we sponsor",
    r"(?-i:\bH-?1B\b)",
    r"(?-i:\b(?:CPT|OPT)\b)",
    r"F-?1 students?",
    r"open to (?:international|f-?1) (?:students|candidates)",
    r"sponsor(?:s|ship for)? (?:employment )?visas?",
]

# --- the trap, stated explicitly (spec 6) -------------------------------------------
# "must be authorized to work in the US" is NOT a block: F-1 with CPT/OPT *is* authorized.
# Keep "authorized to work" out of the blocking list entirely. A clearance requirement, by
# contrast, is a genuine hard block. Do not let one regex conflate the two.
#
# This list is a PERMANENT GUARD, not a runtime filter. `test_eligibility.py` asserts that
# no blocking pattern matches any of these phrases; adding one to a blocking list makes the
# suite fail loudly rather than silently costing you postings you would never see.
NEVER_BLOCK_ON = [
    r"authorized to work",
    r"legally authorized",
    r"work authorization",
    r"authorization to work",
    r"eligible to work",
]

_FLAGS = re.IGNORECASE
_BLOCKING_RES = [re.compile(p, _FLAGS) for p in BLOCKING_PATTERNS]
_POSITIVE_RES = [re.compile(p, _FLAGS) for p in POSITIVE_PATTERNS]

# Sentence boundary. A newline counts: the HTML stripper turns bullets into lines and a
# bullet has no terminal period, so splitting on punctuation alone would run a refusal
# bullet together with the one after it.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")

# Abbreviations whose internal periods are NOT sentence boundaries. Without this, "Must be
# a U.S. citizen." splits into "Must be a U." + "S. citizen." and the single most common
# blocking phrase in the whole corpus stops matching - silently, and in the direction that
# costs you nothing you would notice. Every entry here was a real miss.
_ABBREVIATIONS = re.compile(
    r"\b(?:U\.S\.(?:A\.)?|U\.K\.|E\.U\.|Ph\.D\.|M\.S\.|B\.S\.|D\.C\.|e\.g\.|i\.e\."
    r"|a\.m\.|p\.m\.|Inc\.|Ltd\.|Corp\.|Co\.|St\.|Mr\.|Mrs\.|Ms\.|Dr\.|vs\.|etc\."
    r"|approx\.|No\.)",
    re.IGNORECASE,
)
_DOT = "\x00"  # stand-in for a protected period; never appears in scraped text


def split_sentences(description_text: str) -> list[str]:
    """Sentences, verbatim apart from surrounding whitespace.

    Abbreviation periods are shielded before splitting and restored afterwards, so the
    returned text is byte-identical to the source and still quotable as evidence.
    """
    if not description_text:
        return []
    shielded = _ABBREVIATIONS.sub(lambda m: m.group(0).replace(".", _DOT), description_text)
    return [
        s.replace(_DOT, ".").strip()
        for s in _SENTENCE_SPLIT.split(shielded)
        if s.replace(_DOT, ".").strip()
    ]


def find_sentences(description_text: str, patterns: list[str]) -> list[str]:
    """Return the sentences containing any pattern match, verbatim.

    Verbatim is the whole point: the evidence is what makes a wrong label cost two seconds
    instead of a lost opportunity.

    Matching is scoped to one sentence, so a phrase straddling a boundary ("...we are
    unable to. Sponsor applications close Friday.") does not match. That is by design: a
    phrase spanning two sentences is almost always a coincidence, and a false `blocked` is
    a posting you never see.
    """
    compiled = [re.compile(p, _FLAGS) for p in patterns]
    return [
        sentence
        for sentence in split_sentences(description_text)
        if any(r.search(sentence) for r in compiled)
    ]


def _match(sentences: list[str], compiled: list[re.Pattern[str]]) -> list[str]:
    return [s for s in sentences if any(r.search(s) for r in compiled)]


def apply_override(
    company: str, overrides: dict[str, dict[str, Any]]
) -> tuple[Eligibility, str] | None:
    """Look up `sponsorship_overrides.yaml` (spec 6). Returns (label, note) or None.

    Keyed on `normalize_company`, so "Anduril Industries, Inc." matches an "anduril" entry.
    The note carries the `checked_on` date into the evidence list, so a stale hand-check is
    visible in the alert rather than invisible.
    """
    if not overrides:
        return None
    wanted = normalize_company(company).split()
    for key, block in overrides.items():
        # Token-PREFIX match, not equality: "anduril" has to match "Anduril Industries,
        # Inc.", whose normalized form is "anduril industries". Token-wise rather than
        # string-wise so a "meta" entry does not swallow "metabolic labs".
        key_tokens = normalize_company(key).split()
        if not key_tokens or wanted[: len(key_tokens)] != key_tokens:
            continue
        block = block or {}
        status = block.get("status")
        if status not in ("blocked", "likely_ok"):
            continue
        note = block.get("note") or "no note"
        checked = block.get("checked_on") or "date not recorded"
        return status, f"override for {key}: {status} - {note} (checked {checked})"
    return None


def classify(
    description_text: str, company: str, overrides: dict[str, dict[str, Any]] | None = None
) -> tuple[Eligibility, list[str]]:
    """Return (label, evidence_sentences).

    Order of resolution:
      1. Company-level override, if present. **Overrides beat text classification.**
      2. Blocking signals -> "blocked".
      3. Positive signals -> "likely_ok".
      4. Otherwise "unknown".

    When both a blocking and a positive signal match, the result is "blocked" and BOTH
    sentences are evidence - that combination is usually a multi-req posting or a
    boilerplate collision, and it is exactly the case worth eyeballing.
    """
    override = apply_override(company, overrides or {})
    sentences = split_sentences(description_text)
    blocking = _match(sentences, _BLOCKING_RES)
    positive = _match(sentences, _POSITIVE_RES)

    if override is not None:
        label, note = override
        # The text evidence is kept alongside the override so a wrong override is visible.
        return label, [note, *blocking, *positive]
    if blocking:
        return "blocked", [*blocking, *positive]
    if positive:
        return "likely_ok", positive
    return "unknown", []


def blocking_group(sentence: str) -> str | None:
    """Which family of signal fired, for the audit report. None if nothing matches."""
    for name, patterns in BLOCKING_GROUPS.items():
        if any(re.search(p, sentence, _FLAGS) for p in patterns):
            return name
    return None


# --- known limitations, kept in the code where they will actually be read ------------
#
# * Posting text is a weak proxy. Companies that sponsor routinely often say nothing, and
#   boilerplate refusal language sometimes sits on reqs the recruiter will happily override.
# * Seed the overrides file from public H-1B/PERM disclosure data (DOL LCA filings and the
#   aggregators built on them) plus known defense and government-contractor employers. Once,
#   for the top ~60 targets. One evening, and more reliable than any text heuristic.
# * A regex will occasionally block something winnable. Nothing is deleted, evidence is
#   always shown, `--show-blocked` exists.
# * "Secret clearance" fires on the literal word "secret" next to "clearance" only. A req
#   that says "you will handle confidential information" is not matched, and one that says
#   "clearance preferred" IS - preferred-vs-required is not distinguished here, which errs
#   toward blocking. Audit the blocked list; that is what the gate asks you to do.
