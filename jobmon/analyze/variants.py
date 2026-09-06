"""Resume variant recommendation (spec 9). PHASE 2.

Four hand-written variants, authored once. This replaces per-job generation: four hours of
writing captures most of the value of tailoring, at zero machine-voice risk.

    generalist - broad SWE: coursework, projects, general engineering
    mle        - adversarial robustness honors research, ML coursework
    systems    - Lenahan Lab spectrometer instrument software, PyQt6, hardware control
    security   - ML-based IDS for IoT, adversarial threat model

Build step 0 decoupled the profile from the document. A variant is a block of per-category
weights in `config/variants.yaml`, loaded by `config_loader` into `Config.variants`, and
`pdf` may be null until the document is written. Scoring never opens the PDF.

This is NOT spec 9's term-overlap model. There are no per-variant term lists any more, so
there is nothing to take an overlap against; the posting's own matched categories carry the
signal instead. Ordering behaviour was checked against build-step T5.7 and T5.8.

Explicitly NOT here: LLM-generated per-job resume text. Only ~4 substantive resume items to
reorder, so the ceiling is low, and keyword-driven rewriting drifts into stuffing, which
reads as machine-written.
"""

from __future__ import annotations

from ..models import Variant, VariantProfile

# Tie-break order when two variants score identically. `generalist` first: it is the safe
# default when the posting does not clearly point anywhere (spec 9).
TIE_BREAK = ("generalist", "mle", "systems", "security")

# A recommendation is only asserted when the winner clears both bars. Below either one the
# posting still alerts in full - this gates the RECOMMENDATION, never the alert (spec 6, 9,
# and notify.should_alert, whose suppression rules do not include variant score).
CONFIDENCE_THRESHOLD = 0.70  # absolute score the winner must reach
MIN_LEAD = 0.10  # absolute gap between the winner and the runner-up


def score(
    matched: dict[str, list[str]], variants: dict[str, VariantProfile]
) -> dict[str, float]:
    """Score every variant against one posting's matched keywords.

    Weighted mean of the variant's category weights over the posting's matched-term
    distribution, so scores are comparable across postings of different lengths. The
    divisor is the same for every variant, so it does not change the ranking - it exists so
    CONFIDENCE_THRESHOLD and MIN_LEAD mean something.

    Returns all four scores, not just the winner (spec 9), so a close call can be
    overridden by hand. A category absent from a variant's weights contributes 0 (T0.3).
    """
    total_terms = sum(len(terms) for terms in matched.values())
    if total_terms == 0:
        return {name: 0.0 for name in variants}
    return {
        name: sum(profile.weights.get(cat, 0.0) * len(terms) for cat, terms in matched.items())
        / total_terms
        for name, profile in variants.items()
    }


def recommend(scores: dict[str, float]) -> Variant:
    """The highest-scoring variant. Ties break toward `generalist`, then in TIE_BREAK order.

    This is the raw argmax and says nothing about whether the win is meaningful - an
    all-zero score set returns `generalist` and looks identical to a real win. Always pair
    it with `is_confident`.
    """
    if not scores:
        return "generalist"
    best = max(scores.values())
    winners = [name for name, value in scores.items() if value == best]
    for name in TIE_BREAK:
        if name in winners:
            return name  # type: ignore[return-value]
    return min(winners)  # type: ignore[return-value]


def lead(scores: dict[str, float]) -> float:
    """Gap between the top two scores. 0.0 when fewer than two variants are configured."""
    if len(scores) < 2:
        return 0.0
    top, second = sorted(scores.values(), reverse=True)[:2]
    return top - second


def is_confident(scores: dict[str, float]) -> tuple[bool, str]:
    """Return (confident, reason), matching `notify.should_alert`'s convention.

    False means "the scores do not point anywhere clearly": show all four and let the
    reader pick, rather than presenting a coin-flip as a decision. It does NOT mean the
    posting is suppressed. Nothing here can drop an alert.
    """
    if not scores:
        return False, "no variants configured"
    top = max(scores.values())
    margin = lead(scores)
    if top < CONFIDENCE_THRESHOLD:
        return False, f"top score {top:.2f} below {CONFIDENCE_THRESHOLD:.2f}"
    if margin < MIN_LEAD:
        return False, f"lead {margin:.2f} below {MIN_LEAD:.2f}"
    return True, f"score {top:.2f}, lead {margin:.2f}"


def pdf_note(profile: VariantProfile) -> str:
    """Alert-line label, e.g. "mle (PDF not yet written)" (build step 0)."""
    return f"{profile.name} (PDF not yet written)" if profile.pdf is None else profile.name
