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
# Words that identify a function in a TITLE but are far too common in a body to count
# there: "Software Engineering Intern" is a SWE req, while a body saying "software" or
# "AI" three times says nothing. Measured on the 2026-09-20 ledger, 1,652 of 3,691 live
# new-grad/intern postings (45%) matched no function at all, mostly curated-repo rows with
# no body and titles like "Software Engineering Intern", "Technology Developer Intern",
# "Flight Software Intern" or "Electrical Engineering Co-op" - all of which ended up under
# "unclassified" at the bottom of the digest.
FUNCTION_TITLE_TERMS: dict[Function, list[str]] = {
    "mle": ["ai", "ml", "ai/ml", "artificial intelligence", "applied scientist",
            "generative ai", "genai", "perception", "applied ai"],
    "systems": ["flight software", "avionics", "gpu", "compiler", "compilers",
                "operating systems", "systems software", "silicon", "asic", "vlsi",
                "autonomy", "simulation", "high performance computing", "hpc",
                "systems engineer", "systems engineering", "rtl"],
    "security": ["cyber", "cybersecurity", "cyber security", "red team", "soc analyst"],
    "research": ["research", "researcher"],
    "data": ["data", "business intelligence", "data analyst", "data analytics"],
    "quant": ["quantitative developer", "quantitative researcher"],
    "swe": ["software", "developer", "programmer", "programming", "web", "mobile", "ios",
            "android", "cloud", "devops", "site reliability", "sre", "c++", "java",
            "python", "computer science", "technology developer", "technology analyst",
            "application developer", "applications engineer", "fullstack", "infrastructure",
            "platform", "game", "engineering productivity", "solutions engineer",
            "forward deployed", "react", "react native", "frontend", "technology",
            "information technology", "it", "technical support", "support engineer",
            "user experience engineer", "product engineering", "associate engineer"],
    "hardware": ["electrical", "mechanical", "hardware engineer", "hardware engineering",
                 "manufacturing", "civil", "chemical", "aerospace", "structural",
                 "test engineer", "controls", "rf", "power", "thermal", "materials",
                 "industrial engineering", "process engineer", "quality engineer",
                 "technician", "mechatronics", "propulsion", "construction", "layout",
                 "electronics", "pcb", "circuit", "optical", "nuclear", "analog",
                 "physical design", "design engineer", "device", "plumbing",
                 "environmental", "subsurface", "reliability engineering", "audio"],
    "product": ["product manager", "product management", "product development",
                "product specialist", "product intern", "product analyst",
                "product marketing", "program manager", "program management",
                "project manager", "project management", "ux", "ui/ux", "designer",
                "product design", "technical program", "pm", "developer relations",
                "developer advocate"],
    "business": ["business", "finance", "financial", "accounting", "accountant",
                 "marketing", "sales", "operations", "strategy", "consulting", "consultant",
                 "audit", "tax", "investment", "banking", "risk", "supply chain", "legal",
                 "human resources", "recruiting", "estimating", "procurement", "analyst",
                 "recruiter", "recruitment", "hr", "asset management", "events", "gtm",
                 "leadership development", "customer", "strategist", "personnel security"],
}

# The role noun outranks the domain word. "Strategic Finance, AI & Business Partnership",
# "Product Designer, AI Native" and "Personnel Security Specialist" each matched a
# technical label from a modifier ("AI", "Security"), and since a posting is grouped under
# its highest-ranked label they were listed at the TOP of the digest, as ML and security
# roles. So when a title carries one of these decisive non-software role words and no
# engineering role noun, only the non-software labels are kept. Generic words that also
# name technical roles ("analyst", "business" as in Business Intelligence, "operations")
# are deliberately absent.
_DECISIVE_NON_TECH_RE = _boundary([
    "finance", "financial", "accounting", "accountant", "marketing", "sales", "strategy",
    "strategist", "consulting", "consultant", "audit", "tax", "investment", "banking",
    "legal", "human resources", "recruiting", "recruiter", "recruitment", "hr",
    "procurement", "gtm", "personnel security", "product manager", "product management",
    "program manager", "project manager", "project management", "designer", "ux", "pm",
    "developer relations", "developer advocate", "product marketing", "technical program",
])
_TECH_ROLE_NOUN_RE = re.compile(
    r"(?<!\w)(?:engineer\w*|developer(?!\s+(?:relations|advocate))|scientist|programmer"
    r"|researcher|swe|sde|mle)(?!\w)",
    re.IGNORECASE,
)
_NON_SOFTWARE: frozenset[str] = frozenset({"hardware", "product", "business"})

_FUNCTION_RES: dict[Function, re.Pattern[str]] = {
    fn: _boundary(terms) for fn, terms in FUNCTION_TERMS.items()
}
_FUNCTION_TITLE_RES: dict[Function, re.Pattern[str]] = {
    fn: _boundary(FUNCTION_TERMS.get(fn, []) + terms)
    for fn, terms in FUNCTION_TITLE_TERMS.items()
}

# A title hit is worth this many body hits. Almost every SWE posting says "machine
# learning" somewhere; very few are titled it.
TITLE_WEIGHT = 5
FUNCTION_THRESHOLD = 3

# Tie-break between functions with equal title scores: technical first, so "Software
# Engineer - Business Applications" leads with swe and "Data Analyst" with data, not
# business. Grouping uses the profile's rank, not this; this only orders the list.
FUNCTION_PRECEDENCE: tuple[Function, ...] = (
    "mle", "security", "systems", "swe", "data", "quant", "research",
    "hardware", "product", "business",
)


def classify_function(title: str, description_text: str) -> list[Function]:
    """Function labels, TITLE FIRST (spec 7).

    When the title names a function, the title decides and the body only breaks ties.
    Previously title and body were summed, so a generic SWE req whose body said "security"
    three times was filed as [security, swe] and grouped under security - ahead of swe in
    the usual priority order - which is a large part of why the digest's grouping read as
    wrong. The body alone is consulted only when the title names nothing at all.

    Returns [] rather than defaulting to ["swe"] when nothing matches; an empty function
    list is honest and still alerts.
    """
    title = title or ""
    body = (description_text or "")[:4000]
    body_scores: dict[Function, int] = {
        fn: n for fn, pattern in _FUNCTION_RES.items() if (n := len(pattern.findall(body)))
    }
    title_scores: dict[Function, int] = {}
    for fn, pattern in _FUNCTION_TITLE_RES.items():
        hits = len(pattern.findall(title))
        if hits:
            title_scores[fn] = TITLE_WEIGHT * hits
    for fn, pattern in _FUNCTION_RES.items():
        if fn not in _FUNCTION_TITLE_RES and (hits := len(pattern.findall(title))):
            title_scores[fn] = TITLE_WEIGHT * hits
    if _DECISIVE_NON_TECH_RE.search(title) and not _TECH_ROLE_NOUN_RE.search(title):
        non_software = {fn: s for fn, s in title_scores.items() if fn in _NON_SOFTWARE}
        title_scores = non_software or title_scores

    def order(fn: Function, primary: int) -> tuple[int, int, int]:
        return (-primary, -body_scores.get(fn, 0), FUNCTION_PRECEDENCE.index(fn))

    if title_scores:
        return sorted(title_scores, key=lambda fn: order(fn, title_scores[fn]))
    return sorted(
        (fn for fn, n in body_scores.items() if n >= FUNCTION_THRESHOLD),
        key=lambda fn: order(fn, body_scores[fn]),
    )


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
