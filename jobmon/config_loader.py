"""Loading and shape of the YAML files in `jobmon/config/` (spec 3).

Everything hand-maintained lives in YAML, not in code: company slugs, the keyword taxonomy,
the skill inventory, the sponsorship overrides, and the resume variant profiles. Repo names
roll over by cycle year, so curated-repo URLs are config too (spec 5c).

Validation policy: a typo that would silently shrink the sweep raises. A typo that only
costs signal warns and lets the run continue. A dropped company looks identical to a
company with no openings, which is why the first class stops the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .adapters.ats_public import ENDPOINTS as ATS_ENDPOINTS
from .models import VariantProfile

log = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent / "config"
REPO_ROOT = Path(__file__).resolve().parent.parent

OVERRIDE_STATUSES = frozenset({"blocked", "likely_ok"})
# skill_inventory.yaml key that holds near-substitutes, not a taxonomy category.
EQUIVALENTS_KEY = "equivalents"


class ConfigError(ValueError):
    """A hand-maintained YAML file is wrong in a way that would silently shrink the sweep."""


@dataclass(slots=True)
class Config:
    """Everything loaded from `jobmon/config/`, validated once at startup."""

    companies: dict[str, Any]  # companies.yaml
    taxonomy: dict[str, list[str]]  # taxonomy.yaml: category -> terms
    skill_inventory: dict[str, list[str]]  # skill_inventory.yaml
    sponsorship_overrides: dict[str, dict[str, Any]]  # company -> {status, note, checked_on}
    variants: dict[str, VariantProfile] = field(default_factory=dict)  # variants.yaml
    # addendum 4: normalized company -> {confidence, h1b_filings_recent, source, ...}
    sponsor_history: dict[str, dict[str, Any]] = field(default_factory=dict)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_variants(
    path: Path, taxonomy: dict[str, list[str]] | None = None
) -> dict[str, VariantProfile]:
    """Load `config/variants.yaml` into per-variant weight blocks.

    A null or absent `pdf` is legal (build step 0); a `pdf` that is set but not on disk
    warns and scoring continues (T0.2). Both are warnings, not errors: a missing document
    costs you an attachment, not a posting.

    Pass `taxonomy` to check weight categories against it. A category typo'd in a weights
    block contributes nothing to that variant's score and is invisible otherwise.
    """
    raw = _read_yaml(path)
    profiles: dict[str, VariantProfile] = {}
    for name, block in raw.items():
        block = block or {}
        weights = {str(cat): float(w) for cat, w in (block.get("weights") or {}).items()}
        if taxonomy is not None:
            for cat in weights:
                if cat not in taxonomy:
                    log.warning(
                        "variant %r weights an unknown taxonomy category %r; it will score 0",
                        name,
                        cat,
                    )
        pdf_value = block.get("pdf")
        pdf: Path | None = None
        if pdf_value:
            pdf = Path(pdf_value)
            if not pdf.is_absolute():
                pdf = REPO_ROOT / pdf
            if not pdf.exists():
                log.warning(
                    "variant %r: pdf %s is not on disk; scoring continues, attach by hand",
                    name,
                    pdf,
                )
        profiles[name] = VariantProfile(name=name, weights=weights, pdf=pdf)
    return profiles


def _validate_companies(companies: dict[str, Any]) -> None:
    for i, entry in enumerate(companies.get("ats") or []):
        where = f"companies.yaml ats[{i}]"
        for key in ("company", "ats", "slug"):
            if not entry.get(key):
                raise ConfigError(f"{where}: missing {key!r}")
        if entry["ats"] not in ATS_ENDPOINTS:
            raise ConfigError(
                f"{where}: ats {entry['ats']!r} has no endpoint template; "
                f"known: {sorted(ATS_ENDPOINTS)}"
            )
        if not isinstance(entry.get("tier", False), bool):
            raise ConfigError(f"{where}: tier must be true or false")
    for i, entry in enumerate(companies.get("github_repos") or []):
        if not entry.get("url"):
            raise ConfigError(f"companies.yaml github_repos[{i}]: missing 'url'")


def _validate_skill_inventory(
    inventory: dict[str, Any], taxonomy: dict[str, list[str]]
) -> None:
    for category in inventory:
        if category == EQUIVALENTS_KEY:
            continue
        if category not in taxonomy:
            raise ConfigError(
                f"skill_inventory.yaml: unknown taxonomy category {category!r}; "
                f"known: {sorted(taxonomy)}"
            )


def _validate_overrides(overrides: dict[str, Any]) -> None:
    for company, block in overrides.items():
        status = (block or {}).get("status")
        if status not in OVERRIDE_STATUSES:
            raise ConfigError(
                f"sponsorship_overrides.yaml: {company!r} has status {status!r}; "
                f"must be one of {sorted(OVERRIDE_STATUSES)}"
            )


def load(config_dir: Path = CONFIG_DIR) -> Config:
    """Read and validate every YAML file in `config_dir`."""
    taxonomy = _read_yaml(config_dir / "taxonomy.yaml")
    companies = _read_yaml(config_dir / "companies.yaml")
    skill_inventory = _read_yaml(config_dir / "skill_inventory.yaml")
    overrides = _read_yaml(config_dir / "sponsorship_overrides.yaml")

    _validate_companies(companies)
    _validate_skill_inventory(skill_inventory, taxonomy)
    _validate_overrides(overrides)

    return Config(
        companies=companies,
        taxonomy=taxonomy,
        skill_inventory=skill_inventory,
        sponsorship_overrides=overrides,
        variants=load_variants(config_dir / "variants.yaml", taxonomy),
        sponsor_history=load_sponsor_history(config_dir / "sponsor_history.yaml"),
    )


# --- Phase X: rank to weight ---------------------------------------------------------
#
# Lives here, at config load time, rather than in scoring: the ordered list is a config
# representation and the float is an implementation detail, so the conversion belongs at
# the boundary where every other YAML-to-number conversion happens. Scoring then reads
# plain floats and knows nothing about ranks.

# A category listed in neither `order` nor `never` takes this, so a new taxonomy category
# degrades quietly instead of disappearing (TX.3). It sits below every rank-derived weight
# for any list length, which is what "the lowest non-zero weight" has to mean if it is to
# be stable as you add and remove lines.
_FLOOR = 0.3
_SPREAD = 0.7  # rank 0 scores 1.0; the spread carries the last listed rank down toward 0.3


def rank_weights(
    order: list[str], overrides: dict[str, float] | None = None
) -> dict[str, float]:
    """Map an ordered category list to weights, highest first (Phase X).

    Linear, because nothing here justifies more: `weight = 1.0 - (index / len(order)) * 0.7`.
    You never touch a number unless you want to, and when you do, `overrides` wins for the
    categories it names and leaves every other category on its rank-derived value (TX.4).

    An empty `order` yields an empty map; `Priorities.weight_for` then hands out the floor
    to everything (TX.9).
    """
    weights = {
        category: 1.0 - (index / len(order)) * _SPREAD
        for index, category in enumerate(order)
    }
    for category, value in (overrides or {}).items():
        weights[category] = value
    return weights


rank_weights.FLOOR = _FLOOR  # type: ignore[attr-defined]


# --- addendum 4: employer sponsorship history ----------------------------------------

SPONSOR_HISTORY_PATH = CONFIG_DIR / "sponsor_history.yaml"
SPONSOR_CONFIDENCES = frozenset({"frequent", "occasional", "none_observed", "unknown"})


def load_sponsor_history(path: Path = SPONSOR_HISTORY_PATH) -> dict[str, dict[str, Any]]:
    """Read `config/sponsor_history.yaml`, keyed by normalized company name.

    A missing file is an empty history, not an error: the file is built by a script against
    bulk DOL downloads, and the rest of the system has to work before it has ever been run.

    Keys are normalized on the way in so that a hand-typed "Acme Corp." and the script's
    "acme_corp" both find the same posting's employer. A bad `confidence` value raises -
    it feeds a score term, and a typo there would silently pin an employer at the neutral
    default forever.
    """
    from .normalize import normalize_company

    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    history: dict[str, dict[str, Any]] = {}
    for company, block in raw.items():
        block = dict(block or {})
        confidence = block.get("confidence", "unknown")
        if confidence not in SPONSOR_CONFIDENCES:
            raise ConfigError(
                f"sponsor_history.yaml: {company!r} has confidence {confidence!r}; "
                f"must be one of {sorted(SPONSOR_CONFIDENCES)}"
            )
        block["confidence"] = confidence
        block.setdefault("company", str(company))
        history[normalize_company(str(company).replace("_", " "))] = block
    return history


def tier_companies(cfg: Config) -> set[str]:
    """The ~20 hand-listed companies that get an instant individual alert (spec 10).
    Everything else is batched. Reads `tier: true` off each companies.yaml entry."""
    return {
        entry["company"]
        for entry in (cfg.companies.get("ats") or [])
        if entry.get("tier") is True
    }
