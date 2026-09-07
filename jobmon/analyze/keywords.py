"""Keyword extraction against a hand-curated taxonomy (spec 8). PHASE 2.

Deliberately simple and interpretable. The purpose is **understanding a role in ten
seconds**, not beating an ATS. The ML approach is rejected on purpose: a curated dictionary
is debuggable and a TF-IDF or embedding model is not, at this scale.

Keyword theater warning (spec 14): the ATS keyword-rejection story is oversold, largely by
the companies selling keyword tools. This output is for comprehension. Do not let it drive
resume edits. Nothing here rewrites anything - it reports.
"""

from __future__ import annotations

import re
from typing import Any

from ..filters import role

# Alias -> canonical taxonomy term. The canonical name is what gets reported, never the
# alias that happened to match, so "k8s" and "Kubernetes" are one entry and not two.
ALIASES: dict[str, str] = {
    "cpp": "c++",
    "c plus plus": "c++",
    "c++17": "c++",
    "c++20": "c++",
    "golang": "go",
    "js": "javascript",
    "ts": "typescript",
    "k8s": "kubernetes",
    "rl": "reinforcement learning",
    "deep rl": "reinforcement learning",
    "ml": "machine learning",
    "ids": "intrusion detection",
    "intrusion detection system": "intrusion detection",
    "intrusion detection systems": "intrusion detection",
    "cv": "computer vision",
    "natural language processing": "nlp",
    "large language model": "llm",
    "large language models": "llm",
    "llms": "llm",
    "pytorch lightning": "pytorch",
    "sklearn": "scikit-learn",
    "scikit learn": "scikit-learn",
    "hugging face": "huggingface",
    "finetuning": "fine-tuning",
    "fine tuning": "fine-tuning",
    "pyqt6": "pyqt",
    "pyqt5": "pyqt",
    "amazon web services": "aws",
    "google cloud": "gcp",
    "adversarial robustness": "adversarial",
    "device drivers": "drivers",
    "firmware development": "firmware",
}

# Terms short or common enough that a bare word-boundary match fires on almost every
# posting: "C", "R", "Go", "ML". These require a nearby language/tech cue.
AMBIGUOUS_TERMS = frozenset({"c", "r", "go", "ml", "ray", "jax", "ear"})

# Cue words that make a bare "C" or "Go" mean the language rather than the English word.
_CUE = re.compile(
    r"\b(?:languages?|programming|proficien\w*|fluent|experience (?:in|with)|coding|code"
    r"|develop\w*|written in|familiar\w* with|expertise|skills?|stack)\b",
    re.IGNORECASE,
)
CUE_WINDOW = 60  # characters either side of the match

# English phrases that contain an ambiguous term and are never the technology. Measured on
# real postings: the cue window alone still let "drive global go-to-market programmes" and
# "when something breaks, you go to root cause" through as the Go language, because
# "develop" and "breaks" sit nearby. A false gap says you lack a skill the posting never
# asked for, which is noise in the one output that is supposed to be signal.
_STOP_PHRASES = re.compile(
    r"go[-\s]to[-\s]market|go[-\s]live|go to root|on the go|go deep|go above"
    r"|go the extra|let[^.]{0,20}go|good to go|r[-\s]?&[-\s]?d",
    re.IGNORECASE,
)


def _term_pattern(term: str) -> re.Pattern[str]:
    """Word-boundary matcher for one term. `\\b` fails on "c++" (the trailing + is not a
    word char), so boundaries are asserted with lookarounds instead."""
    return re.compile(rf"(?<![\w+#]){re.escape(term)}(?![\w+#])", re.IGNORECASE)


def _has_cue(text: str, start: int, end: int) -> bool:
    """Whether an ambiguous match looks like the technology rather than the English word.

    Two conditions: a language/tech cue nearby, and the match not sitting inside a known
    stop phrase. The stop check wins - "go-to-market" has "develop" in its window often
    enough that the cue alone is not enough.
    """
    near = text[max(0, start - 30) : end + 30]
    for stop in _STOP_PHRASES.finditer(near):
        # Does the stop phrase actually cover the matched term?
        offset = start - max(0, start - 30)
        if stop.start() <= offset < stop.end():
            return False
    window = text[max(0, start - CUE_WINDOW) : end + CUE_WINDOW]
    return bool(_CUE.search(window))


def match(
    description_text: str, title: str, taxonomy: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Matched terms grouped by taxonomy category. The ten-second summary of the role.

    Category order from the YAML is preserved so the summary line reads consistently.
    Aliases resolve to the canonical term. Ambiguous one- and two-character terms ("C",
    "R", "Go") only count with a language cue nearby - without that guard they match
    essentially every posting and the whole output becomes noise.
    """
    haystack = f"{title}\n{description_text or ''}"
    # alias -> canonical, restricted to terms this taxonomy actually contains
    known = {t for terms in taxonomy.values() for t in terms}
    lookup: dict[str, str] = {t: t for t in known}
    lookup.update({a: c for a, c in ALIASES.items() if c in known})

    found: dict[str, set[str]] = {}
    canonical_to_category = {
        term: cat for cat, terms in taxonomy.items() for term in terms
    }
    for surface, canonical in lookup.items():
        category = canonical_to_category.get(canonical)
        if category is None:
            continue
        for m in _term_pattern(surface).finditer(haystack):
            if surface.lower() in AMBIGUOUS_TERMS and not _has_cue(haystack, m.start(), m.end()):
                continue
            found.setdefault(category, set()).add(canonical)
            break
    return {cat: sorted(found[cat]) for cat in taxonomy if cat in found}


def gap_list(
    matched: dict[str, list[str]], skill_inventory: dict[str, Any]
) -> list[dict[str, str]]:
    """Taxonomy terms present in the posting and absent from `skill_inventory.yaml`.

    The genuinely useful output (spec 8): it says whether a gap is *real* (no Kubernetes
    anywhere in my history) or *cosmetic* (they said TensorFlow, I have PyTorch).

    Returns dicts of {term, category, type, substitute}. `type` is "real" or "cosmetic";
    `substitute` names the thing I have instead, and is "" for a real gap. Cosmetic gaps
    are marked, never hidden - the distinction IS the value, and collapsing it makes the
    list noise.

    A term is cosmetic only when its `equivalents` entry is ITSELF in the inventory. An
    equivalents map trusted without that check happily calls a gap cosmetic on the strength
    of a substitute you also do not have.
    """
    equivalents = {
        str(k).lower(): str(v).lower()
        for k, v in (skill_inventory.get("equivalents") or {}).items()
    }
    held = {
        term.lower()
        for key, terms in skill_inventory.items()
        if key != "equivalents" and isinstance(terms, list)
        for term in terms
    }

    gaps: list[dict[str, str]] = []
    for category, terms in matched.items():
        for term in terms:
            if term.lower() in held:
                continue
            substitute = equivalents.get(term.lower(), "")
            cosmetic = bool(substitute) and substitute in held
            gaps.append({
                "term": term,
                "category": category,
                "type": "cosmetic" if cosmetic else "real",
                "substitute": substitute if cosmetic else "",
            })
    return gaps


def summary_line(matched: dict[str, list[str]], max_len: int = 140) -> str:
    """One line for the alert payload, e.g.
    "python, pytorch, distributed training | k8s, aws | phd preferred".

    Capped: an alert that wraps five lines on a phone is not a ten-second read.
    """
    parts = [", ".join(terms) for terms in matched.values() if terms]
    line = " | ".join(parts)
    if len(line) <= max_len:
        return line
    return line[: max_len - 1].rsplit(", ", 1)[0] + " ..."


def format_gaps(gaps: list[dict[str, str]]) -> str:
    """Gap list for the alert, real gaps first - those are the ones worth reading."""
    if not gaps:
        return ""
    ordered = sorted(gaps, key=lambda g: (g["type"] != "real", g["term"]))
    return ", ".join(
        g["term"] if g["type"] == "real" else f"{g['term']} (~{g['substitute']})"
        for g in ordered
    )


def degree_requirement(description_text: str) -> tuple[bool, str | None]:
    """Return (is_hard_floor, matched_text) for MS/PhD requirements (spec 8).

    Relevant for MLE reqs specifically, where a hard MS/PhD floor is common. Those get
    labeled `degree_gated` and deprioritized - never suppressed.

    The decision itself lives in `filters/role.is_degree_gated`, which Phase 1 already
    ships and tests; this adds the matched sentence for the alert rather than
    reimplementing the rule. Two copies of "is a PhD required" would drift apart.
    """
    gated = role.is_degree_gated(description_text)
    if not gated:
        return False, None
    for sentence in re.split(r"(?<=[.!?])\s+|\n", description_text or ""):
        if role.is_degree_gated(sentence):
            return True, sentence.strip()
    return True, None


def yoe_floor(description_text: str) -> int | None:
    """Stated years-of-experience minimum (spec 8). Delegates to `filters/role` for the
    same reason `degree_requirement` does."""
    return role.extract_yoe_floor(description_text)
