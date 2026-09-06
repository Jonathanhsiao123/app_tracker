"""Config loading and validation (spec 3).

Validation policy under test: a typo that would silently shrink the sweep raises; a typo
that only costs signal warns. A company dropped by a typo looks exactly like a company with
no openings, which is the failure this file exists to prevent.
"""

import textwrap

import pytest

from jobmon import config_loader
from jobmon.config_loader import Config, ConfigError


def write_config(tmp_path, **files):
    defaults = {
        "taxonomy.yaml": "languages: [python]\nml_topics: [adversarial]\n",
        "companies.yaml": "ats: []\n",
        "skill_inventory.yaml": "languages: []\n",
        "sponsorship_overrides.yaml": "",
        "variants.yaml": "generalist:\n  pdf: null\n  weights: {languages: 1.0}\n",
    }
    defaults.update(files)
    for name, body in defaults.items():
        (tmp_path / name).write_text(textwrap.dedent(body), encoding="utf-8")
    return tmp_path


def test_shipped_config_loads():
    cfg = config_loader.load()
    assert isinstance(cfg, Config)
    assert sorted(cfg.variants) == ["generalist", "mle", "security", "systems"]


def test_empty_overrides_file_is_legal(tmp_path):
    """`sponsorship_overrides.yaml` is empty now; the code path must work anyway (T4.3)."""
    cfg = config_loader.load(write_config(tmp_path))
    assert cfg.sponsorship_overrides == {}


def test_missing_config_file_raises(tmp_path):
    d = write_config(tmp_path)
    (d / "taxonomy.yaml").unlink()
    with pytest.raises(ConfigError, match="missing config file"):
        config_loader.load(d)


# --- tier, renamed from tier1 ------------------------------------------------------

def test_tier_companies_reads_tier_true(tmp_path):
    cfg = config_loader.load(
        write_config(
            tmp_path,
            **{
                "companies.yaml": """
                ats:
                  - {company: Acme, ats: greenhouse, slug: acme, tier: true}
                  - {company: Beta, ats: lever, slug: beta, tier: false}
                  - {company: Gamma, ats: ashby, slug: gamma}
                """
            },
        )
    )
    assert config_loader.tier_companies(cfg) == {"Acme"}


def test_non_boolean_tier_raises(tmp_path):
    d = write_config(
        tmp_path,
        **{"companies.yaml": "ats:\n  - {company: Acme, ats: greenhouse, slug: acme, tier: yes please}\n"},
    )
    with pytest.raises(ConfigError, match="tier must be"):
        config_loader.load(d)


# --- loud failures -----------------------------------------------------------------

def test_unknown_ats_raises(tmp_path):
    d = write_config(
        tmp_path,
        **{"companies.yaml": "ats:\n  - {company: Acme, ats: workday, slug: acme, tier: false}\n"},
    )
    with pytest.raises(ConfigError, match="no endpoint template"):
        config_loader.load(d)


def test_company_entry_missing_slug_raises(tmp_path):
    d = write_config(
        tmp_path, **{"companies.yaml": "ats:\n  - {company: Acme, ats: greenhouse}\n"}
    )
    with pytest.raises(ConfigError, match="missing 'slug'"):
        config_loader.load(d)


def test_unknown_skill_inventory_category_raises(tmp_path):
    d = write_config(tmp_path, **{"skill_inventory.yaml": "langauges: [python]\n"})
    with pytest.raises(ConfigError, match="unknown taxonomy category"):
        config_loader.load(d)


def test_equivalents_block_is_not_a_taxonomy_category(tmp_path):
    cfg = config_loader.load(
        write_config(
            tmp_path,
            **{"skill_inventory.yaml": "languages: [python]\nequivalents:\n  tensorflow: pytorch\n"},
        )
    )
    assert cfg.skill_inventory["equivalents"] == {"tensorflow": "pytorch"}


def test_unknown_override_status_raises(tmp_path):
    d = write_config(
        tmp_path,
        **{"sponsorship_overrides.yaml": "Acme:\n  status: maybe\n  note: n\n  checked_on: 2026-09-06\n"},
    )
    with pytest.raises(ConfigError, match="must be one of"):
        config_loader.load(d)


@pytest.mark.parametrize("status", ["blocked", "likely_ok"])
def test_valid_override_statuses_load(tmp_path, status):
    cfg = config_loader.load(
        write_config(
            tmp_path,
            **{"sponsorship_overrides.yaml": f"Acme:\n  status: {status}\n  note: n\n"},
        )
    )
    assert cfg.sponsorship_overrides["Acme"]["status"] == status


def test_shipped_ats_endpoints_cover_every_documented_ats():
    """Spec 5a names seven public-ATS sources. All seven have an endpoint template."""
    from jobmon.adapters.ats_public import ENDPOINTS

    assert sorted(ENDPOINTS) == [
        "ashby",
        "greenhouse",
        "lever",
        "personio",
        "recruitee",
        "smartrecruiters",
        "workable",
    ]
