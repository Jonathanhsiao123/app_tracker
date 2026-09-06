"""Canonical records shared by every stage of the pipeline (spec 4).

Spec 4 writes these as dicts. They are dataclasses here so adapters fail at construction
time instead of at notification time, and so `locale` cannot be forgotten (spec 13: keep
the schema market-agnostic from day one). Serialize with `dataclasses.asdict` at the
storage boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

RemoteType = Literal["remote", "hybrid", "onsite"]
RoleClass = Literal["new_grad", "internship", "experienced", "unclear"]
Eligibility = Literal["blocked", "likely_ok", "unknown"]
Variant = Literal["generalist", "mle", "systems", "security"]
Function = Literal["swe", "mle", "systems", "security", "research", "data", "quant"]
ApplicationStatus = Literal["applied", "screen", "reject", "onsite", "offer"]


@dataclass(slots=True)
class Posting:
    """One job posting, normalized. Every adapter produces exactly this."""

    global_id: str  # "{source}:{company}:{source_job_id}" - primary key
    source: str  # "greenhouse" | "lever" | "google" | "github_repo" | ...
    company: str
    title: str
    locations: list[str]
    description_html: str
    description_text: str  # stripped; all analysis reads this, never the HTML
    apply_url: str
    first_seen_at: datetime
    last_seen_at: datetime
    remote_type: RemoteType | None = None
    posted_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)  # untouched payload, for debugging
    locale: str = "US"  # spec 13; "US" until Phase 4 exists

    @staticmethod
    def make_global_id(source: str, company: str, source_job_id: str) -> str:
        """Build the primary key. TODO: slugify company (case, punctuation, legal suffixes)
        so "Acme, Inc." and "acme inc" do not produce two keys for one company."""
        raise NotImplementedError


@dataclass(slots=True)
class Analysis:
    """Derived record, written by the analyzer. One row per posting."""

    global_id: str
    role_class: RoleClass
    function: list[Function]
    eligibility: Eligibility
    eligibility_evidence: list[str]  # matched sentences, VERBATIM (spec 6)
    keywords: dict[str, list[str]]  # taxonomy category -> matched terms
    gaps: list[str]  # taxonomy terms in posting, absent from skill_inventory.yaml
    recommended_variant: Variant
    variant_scores: dict[str, float]  # all four, so a close call can be overridden
    grad_window: str | None = None  # e.g. "December 2026 and August 2027"
    degree_gated: bool = False  # hard MS/PhD floor -> deprioritize, never suppress
    yoe_floor: int | None = None  # stated years-of-experience minimum


@dataclass(slots=True)
class AdapterHealth:
    """One line per adapter per run (spec 5b). Zero results with no error is the failure
    mode that matters: silent breakage. Surfaced by `jobmon health` and the weekly summary."""

    adapter: str
    run_at: datetime
    ok: bool
    postings_seen: int
    new_postings: int
    duration_ms: int
    error: str | None = None


@dataclass(slots=True)
class VariantProfile:
    """One resume variant: per-taxonomy-category weights, and the PDF once it exists.

    Lives here rather than in `analyze/variants.py` so `config_loader` can build one without
    importing the analyzer (spec 9, build step 0). `pdf` stays None until the document is
    written; nothing in scoring ever opens it.
    """

    name: str
    weights: dict[str, float] = field(default_factory=dict)
    pdf: Path | None = None
