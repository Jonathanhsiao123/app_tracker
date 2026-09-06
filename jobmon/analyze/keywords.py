"""Keyword extraction against a hand-curated taxonomy (spec 8). PHASE 2.

Deliberately simple and interpretable. The purpose is **understanding a role in ten
seconds**, not beating an ATS. The ML approach is rejected on purpose: a curated dictionary
is debuggable and a TF-IDF or embedding model is not, at this scale.

Keyword theater warning (spec 14): the ATS keyword-rejection story is oversold, largely by
the companies selling keyword tools. This output is for comprehension. Do not let it drive
resume edits.
"""

from __future__ import annotations


def match(description_text: str, title: str, taxonomy: dict[str, list[str]]) -> dict[str, list[str]]:
    """Matched terms grouped by taxonomy category. The ten-second summary of the role.

    TODO:
      - Word-boundary matching, case-insensitive, but "C" and "Go" and "R" need special
        handling or every posting matches them. Require an adjacent language cue for the
        one- and two-character terms.
      - Handle aliases as one term: "rl" / "reinforcement learning", "k8s" / "kubernetes",
        "c++" / "cpp". Report the canonical name, not the alias that matched.
      - Preserve category order from the YAML so the summary line reads consistently.
    """
    raise NotImplementedError


def gap_list(matched: dict[str, list[str]], skill_inventory: dict[str, list[str]]) -> list[str]:
    """Taxonomy terms present in the posting and absent from `skill_inventory.yaml`.

    The genuinely useful output (spec 8): it says whether a gap is *real* (no Kubernetes
    anywhere in my history) or *cosmetic* (they said TensorFlow, I have PyTorch).

    TODO: cosmetic gaps need a sibling notion in the taxonomy - terms in the same category
    that are near-substitutes. Mark those "cosmetic" in the output rather than hiding them;
    the distinction is the value, and collapsing it makes the list noise.
    """
    raise NotImplementedError


def summary_line(matched: dict[str, list[str]]) -> str:
    """One line for the alert payload, e.g.
    "python, pytorch, distributed training | k8s, aws | phd preferred".
    TODO: cap the length - an alert that wraps five lines on a phone is not a ten-second read."""
    raise NotImplementedError


def degree_requirement(description_text: str) -> tuple[bool, str | None]:
    """Return (is_hard_floor, matched_text) for MS/PhD requirements (spec 8).

    Relevant for MLE reqs specifically, where a hard MS/PhD floor is common. Those get
    labeled `degree_gated` and deprioritized - never suppressed.

    TODO: "MS required" vs "MS preferred" vs "MS or equivalent experience" (not a floor)
    vs "PhD in progress" (an intern req, often fine).
    """
    raise NotImplementedError
