"""Analyzer: posting text -> the `Analysis` record (spec 4, 8, 9). PHASE 2.

`keywords.py` - taxonomy matching, degree gate, gap list
`variants.py` - resume variant scoring

The system reports gaps. **It never rewrites anything to close them.**
"""

from __future__ import annotations

from ..config_loader import Config
from ..models import Analysis, Posting


def analyze(posting: Posting, cfg: Config) -> Analysis:
    """Run every filter and analyzer over one posting and assemble the derived record.

    Composes: `filters.role.classify_role` / `classify_function` / `extract_grad_window`
    / `extract_yoe_floor` / `is_degree_gated`, `filters.eligibility.classify`,
    `keywords.match` / `gap_list`, and `variants.score`.

    TODO: pure function of (posting, config) - no DB, no network. That is what makes a
    re-analysis after a taxonomy edit cheap and re-runnable over the whole table.
    """
    raise NotImplementedError
