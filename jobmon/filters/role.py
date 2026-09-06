"""Role classification (spec 7). PHASE 1.

`unclear` goes through to alerts rather than being dropped. A missed posting costs more
than a false alert.

Everything here matches on word boundaries. Substring matching fails "Internal Tools" and
"International Payments" as internships and "Principal Investigator" as seniority, which is
testbench T3b's entire point.
"""

from __future__ import annotations

import re

from ..models import Function, RoleClass

INTERNSHIP_MARKERS = [
    "intern", "interns", "internship", "internships", "co-op", "coop", "summer 2027",
    "summer analyst", "industrial placement",
]

NEW_GRAD_MARKERS = [
    "new grad", "new grads", "new graduate", "university graduate", "entry level",
    "entry-level", "campus", "early career", "graduate software engineer",
    "college graduate", "recent graduate", "university hire", "grad program",
    "rotational program",
]

# Seniority markers that reject a posting outright.
SENIORITY_REJECTS = [
    "senior", "sr", "staff", "principal", "lead", "manager", "director", "architect",
    "head of", "vp", "distinguished", "fellow",
]

# "II"/"III" only reject when paired with a YoE floor above 2 - at some companies a
# "Software Engineer II" IS the new grad level, so this stays a separate, softer rule.
LEVEL_SUFFIXES = ["ii", "iii", "iv"]

# A YoE floor at or below this does not exclude a new grad.
YOE_TOLERANCE = 2


def _boundary(terms: list[str]) -> re.Pattern[str]:
    """Word-boundary alternation. Terms containing '-' are matched literally, so 'co-op'
    does not also match 'coop' by accident and vice versa - both are listed explicitly."""
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)", re.IGNORECASE)


_INTERNSHIP_RE = _boundary(INTERNSHIP_MARKERS)
_NEW_GRAD_RE = _boundary(NEW_GRAD_MARKERS)
_SENIORITY_RE = _boundary(SENIORITY_REJECTS)
_LEVEL_RE = _boundary(LEVEL_SUFFIXES)


def classify_role(title: str, description_text: str) -> RoleClass:
    """Return "new_grad" | "internship" | "experienced" | "unclear" (spec 7).

    The title is weighted far above the description: almost every posting's body mentions
    a senior engineer somewhere ("you will be mentored by senior engineers"), and reading
    seniority out of the body rejects real new grad roles.

    Precedence: seniority in the TITLE rejects first, before internship or new grad markers
    can fire. "Senior Software Engineer, New Grad Program" is a senior req with a program
    name attached, and T3b requires it rejected.
    """
    title = title or ""
    body = description_text or ""

    if _SENIORITY_RE.search(title):
        return "experienced"
    if _LEVEL_RE.search(title):
        floor = extract_yoe_floor(body)
        if floor is not None and floor > YOE_TOLERANCE:
            return "experienced"
    if _INTERNSHIP_RE.search(title) or _INTERNSHIP_RE.search(body[:600]):
        return "internship"
    if _NEW_GRAD_RE.search(title) or _NEW_GRAD_RE.search(body):
        return "new_grad"

    floor = extract_yoe_floor(body)
    if floor is not None and floor > YOE_TOLERANCE:
        return "experienced"
    # A title with no seniority marker plus a stated graduation window is a new grad req.
    if extract_grad_window(body):
        return "new_grad"
    return "unclear"


# Sentence-scoped so the returned text is quotable in an alert.
_GRAD_WINDOW_RE = re.compile(
    r"[^.!?\n]*\b(?:graduat\w*|conferral|degree completion|complete[sd]? your degree)\b"
    r"[^.!?\n]*\b(?:20\d{2}|dec\w*|may|june|aug\w*)\b[^.!?\n]*[.!?]?",
    re.IGNORECASE,
)


def extract_grad_window(description_text: str) -> str | None:
    """Pull the graduation-window sentence, e.g.
    "must graduate between December 2026 and August 2027".

    Returned verbatim rather than parsed into a date range: the phrasings vary too much
    ("expected graduation date between...", "graduating in 2027", "conferral no later than
    June 2027") and a wrong parse is worse than a quoted string (spec 7).
    """
    if not description_text:
        return None
    m = _GRAD_WINDOW_RE.search(description_text)
    if not m:
        return None
    return " ".join(m.group(0).split()).strip() or None


_YOE_RE = re.compile(
    r"(?<!\w)(?:(?:minimum|at least|min\.?)\s+(?:of\s+)?)?(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*"
    r"(?:\+\s*)?years?\b(?:[^.\n]{0,40}?\bexperience\b)?",
    re.IGNORECASE,
)
_YOE_NEGATIVE = re.compile(
    r"\bno\s+(?:prior\s+)?experience\s+(?:is\s+)?(?:required|necessary)\b", re.IGNORECASE
)
_DEGREE_YEARS = re.compile(r"\b\d\s*-?\s*year\s+(?:degree|program|course|university)\b", re.IGNORECASE)


def extract_yoe_floor(description_text: str) -> int | None:
    """Stated years-of-experience minimum, if any (spec 8).

    Returns the LOWEST stated floor, not the highest: a posting saying "2+ years required,
    5+ years preferred" has a floor of 2. Ranges starting at 0 or 1 ("0-2 years") do not
    exclude a new grad and are reported as stated. "4-year degree" is not experience.
    """
    if not description_text or _YOE_NEGATIVE.search(description_text):
        return None
    text = _DEGREE_YEARS.sub(" ", description_text)
    floors = [int(m.group(1)) for m in _YOE_RE.finditer(text) if int(m.group(1)) <= 30]
    return min(floors) if floors else None


FUNCTION_TERMS: dict[Function, list[str]] = {
    "mle": ["machine learning", "ml engineer", "mle", "deep learning", "nlp", "llm",
            "computer vision", "pytorch", "tensorflow", "ai engineer"],
    "systems": ["embedded", "firmware", "rtos", "device driver", "drivers", "fpga",
                "hardware", "instrumentation", "dsp", "kernel", "robotics"],
    "security": ["security", "appsec", "infosec", "cryptography", "threat", "malware",
                 "intrusion detection", "penetration", "vulnerability"],
    "research": ["research scientist", "research engineer", "research intern", "phd"],
    "data": ["data engineer", "data science", "data scientist", "analytics", "etl"],
    "quant": ["quant", "quantitative", "trading", "trader", "systematic"],
    "swe": ["software engineer", "software developer", "swe", "backend", "back-end",
            "frontend", "front-end", "full stack", "full-stack", "platform engineer",
            "infrastructure engineer", "software development"],
}
_FUNCTION_RES: dict[Function, re.Pattern[str]] = {
    fn: _boundary(terms) for fn, terms in FUNCTION_TERMS.items()
}

# A title hit is worth this many body hits. Almost every SWE posting says "machine
# learning" somewhere; very few are titled it.
TITLE_WEIGHT = 5
FUNCTION_THRESHOLD = 3


def classify_function(title: str, description_text: str) -> list[Function]:
    """Function labels from title plus description weighting (spec 7).

    Returns [] rather than defaulting to ["swe"] when nothing matches; an empty function
    list is honest and still alerts.
    """
    title = title or ""
    body = (description_text or "")[:4000]
    scores: dict[Function, int] = {}
    for fn, pattern in _FUNCTION_RES.items():
        score = TITLE_WEIGHT * len(pattern.findall(title)) + len(pattern.findall(body))
        if score:
            scores[fn] = score
    return [fn for fn, score in sorted(scores.items(), key=lambda kv: -kv[1])
            if score >= FUNCTION_THRESHOLD]


_DEGREE_GATE_RE = re.compile(
    r"(?<!\w)(?:ms|m\.s\.|msc|master'?s?|phd|ph\.d\.|doctorate)\b[^.\n]{0,60}?"
    r"\b(?:required|require[sd]?|must have|mandatory)\b"
    r"|\b(?:required|must have)\b[^.\n]{0,60}?"
    r"(?<!\w)(?:ms|m\.s\.|msc|master'?s?|phd|ph\.d\.|doctorate)\b",
    re.IGNORECASE,
)
_DEGREE_PREFERRED_RE = re.compile(
    r"(?<!\w)(?:ms|msc|master'?s?|phd|ph\.d\.)\b[^.\n]{0,40}?"
    r"\b(?:preferred|a plus|nice to have|desirable)\b",
    re.IGNORECASE,
)


def is_degree_gated(description_text: str) -> bool:
    """Hard MS/PhD floor (spec 8). Common on MLE reqs.

    "PhD preferred" is not a gate. Gated postings are deprioritized in the digest, never
    suppressed. A sentence carrying both required and preferred language counts as gated.
    """
    if not description_text:
        return False
    for sentence in re.split(r"(?<=[.!?])\s+|\n", description_text):
        if _DEGREE_GATE_RE.search(sentence) and not _DEGREE_PREFERRED_RE.search(sentence):
            return True
    return False
