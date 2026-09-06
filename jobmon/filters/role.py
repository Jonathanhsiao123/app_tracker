"""Role classification (spec 7). PHASE 1.

`unclear` goes through to alerts rather than being dropped. A missed posting costs more
than a false alert.
"""

from __future__ import annotations

from ..models import Function, RoleClass

INTERNSHIP_MARKERS = [
    "intern", "internship", "co-op", "coop", "summer 2027",
]

NEW_GRAD_MARKERS = [
    "new grad", "new graduate", "university graduate", "entry level", "entry-level",
    "campus", "early career", "graduate software engineer", "college graduate",
]

# Seniority markers that reject a posting outright.
SENIORITY_REJECTS = [
    "senior", "staff", "principal", "lead", "manager", "director", "architect",
]

# "II"/"III" only reject when paired with a YoE floor above 2 - at some companies a
# "Software Engineer II" IS the new grad level. TODO: keep this as a separate, softer rule.
LEVEL_SUFFIXES = ["ii", "iii", "iv"]


def classify_role(title: str, description_text: str) -> RoleClass:
    """Return "new_grad" | "internship" | "experienced" | "unclear".

    Rules (spec 7):
      internship  - any INTERNSHIP_MARKERS hit.
      new_grad    - any NEW_GRAD_MARKERS hit, OR a title with no seniority marker plus a
                    stated graduation window.
      experienced - a SENIORITY_REJECTS hit, or II/III with a YoE floor above 2.
      unclear     - everything else. Alerted, not dropped.

    TODO: match on word boundaries. "internal", "international" and "principal
    investigator" must not fire; neither must "senior" inside "Senior Engineer will mentor
    you" in the body - which is why the title is weighted far above the description here.
    """
    raise NotImplementedError


def extract_grad_window(description_text: str) -> str | None:
    """Pull the graduation-window sentence, e.g.
    "must graduate between December 2026 and August 2027".

    This governs actual eligibility more than the title does, and it varies by company and
    sometimes by req (spec 7), so it is surfaced in the alert.

    TODO: return the matched text verbatim rather than a parsed date range - the phrasings
    are too varied ("expected graduation date between...", "graduating in 2027",
    "conferral no later than June 2027") and a wrong parse is worse than a quoted string.
    """
    raise NotImplementedError


def extract_yoe_floor(description_text: str) -> int | None:
    """Stated years-of-experience minimum, if any (spec 8).
    TODO: "3+ years", "minimum of 5 years", "3-5 years". Ignore "0-2 years" style ranges
    that do not actually exclude a new grad, and ignore years attached to a degree
    ("4-year degree")."""
    raise NotImplementedError


def classify_function(title: str, description_text: str) -> list[Function]:
    """Function labels from title plus description weighting (spec 7).

    One of swe / mle / systems / security / research / data / quant, and a posting can
    carry more than one (["swe", "security"]).

    TODO: title terms count for much more than body terms - almost every SWE posting
    mentions "machine learning" somewhere. Return [] rather than defaulting to ["swe"]
    when nothing matches; an empty function list is honest and still alerts.
    """
    raise NotImplementedError


def is_degree_gated(description_text: str) -> bool:
    """Hard MS/PhD floor (spec 8). Common on MLE reqs.
    TODO: distinguish required from preferred - "PhD preferred" is not a gate. Gated
    postings are deprioritized in the digest, never suppressed."""
    raise NotImplementedError
