"""Employer sponsorship history, and how it combines with the text classifier.

Testbench T8.4 and T8.5.

T8.5 is one of the two the addendum says actually matter: it protects the classifier's one
reliable signal. History is a prior. An explicit refusal is not.
"""

import textwrap

import pytest

from jobmon import config_loader
from jobmon.analyze import sponsorship
from jobmon.config_loader import ConfigError

HISTORY = {
    "acme": {"confidence": "frequent", "h1b_filings_recent": 240,
             "source": "dol_lca_disclosure", "since": 2024},
    "midco": {"confidence": "occasional", "h1b_filings_recent": 12, "since": 2024},
    "tinyco": {"confidence": "none_observed", "h1b_filings_recent": 2},
    "acme defense": {"confidence": "none_observed", "h1b_filings_recent": 0,
                     "note": "defense subsidiary does not sponsor"},
}


# --- the combination table, row by row ------------------------------------------------

def test_t8_4_unknown_text_plus_frequent_history_is_promoted_with_named_evidence():
    """T8.4. `unknown` + `frequent` -> `likely_ok`, and the evidence names both the filing
    count and the absence of posting language.

    The evidence sentence is the whole reason the promotion is trustworthy: a label with no
    stated basis is one you cannot overrule in two seconds.
    """
    label, confidence, evidence = sponsorship.combine("unknown", "Acme Corp", HISTORY)
    assert label == "likely_ok"
    assert confidence == "frequent"
    assert "240" in evidence
    assert "no posting language" in evidence
    assert "2024" in evidence


def test_t8_5_blocked_text_plus_frequent_history_stays_blocked():
    """T8.5, MANDATORY. History NEVER overrides an explicit refusal.

    The one thing the text classifier is reliably good at is spotting a refusal, and no
    amount of filing history should overrule "Must be a U.S. citizen."
    """
    label, confidence, evidence = sponsorship.combine("blocked", "Acme Corp", HISTORY)
    assert label == "blocked"
    assert confidence == "frequent", "the confidence is still reported, it just does not win"
    assert evidence is None, "no filing count recited next to a citizenship requirement"


def test_likely_ok_text_stays_likely_ok_whatever_the_history():
    for company in ("Acme Corp", "TinyCo", "Nobody Inc"):
        label, _, _ = sponsorship.combine("likely_ok", company, HISTORY)
        assert label == "likely_ok"


def test_unknown_plus_occasional_stays_unknown_with_the_confidence_noted():
    label, confidence, evidence = sponsorship.combine("unknown", "MidCo", HISTORY)
    assert label == "unknown"
    assert confidence == "occasional"
    assert "occasional" in evidence and "12" in evidence


def test_unknown_plus_none_observed_stays_unknown_and_is_flagged_as_weak():
    """`none_observed` is weak evidence, not a block. Small companies that would sponsor
    simply have not yet."""
    label, confidence, evidence = sponsorship.combine("unknown", "TinyCo", HISTORY)
    assert label == "unknown"
    assert confidence == "none_observed"
    assert "no observed H-1B history" in evidence
    assert "not a block" in evidence


def test_unknown_with_no_history_entry_stays_unknown_with_nothing_to_say():
    label, confidence, evidence = sponsorship.combine("unknown", "Nobody Inc", HISTORY)
    assert (label, confidence, evidence) == ("unknown", "unknown", None)


def test_an_empty_history_file_changes_nothing():
    """The rest of the system has to work before the builder script has ever been run."""
    for text in ("unknown", "likely_ok", "blocked"):
        label, confidence, _ = sponsorship.combine(text, "Acme Corp", {})
        assert label == text
        assert confidence == "unknown"


# --- name matching ----------------------------------------------------------------------

def test_lookup_is_a_token_prefix_match_not_a_substring_one():
    assert sponsorship.lookup("Acme Corporation, Inc.", HISTORY)["confidence"] == "frequent"
    assert sponsorship.lookup("Acmetronics", HISTORY) is None, "not a token prefix"
    assert sponsorship.lookup("Not Acme", HISTORY) is None, "prefix, not contains"


def test_the_longest_matching_prefix_wins_so_subsidiaries_get_their_own_entry():
    """`a big employer may appear under several legal entities` - the defense subsidiary
    is exactly where the parent's record is wrong."""
    parent = sponsorship.lookup("Acme Corp", HISTORY)
    sub = sponsorship.lookup("Acme Defense Systems", HISTORY)
    assert parent["confidence"] == "frequent"
    assert sub["confidence"] == "none_observed"
    assert sponsorship.combine("unknown", "Acme Defense Systems", HISTORY)[0] == "unknown"


# --- the badge ----------------------------------------------------------------------------

def test_the_badge_is_one_line_and_states_the_basis():
    assert sponsorship.badge("likely_ok", "frequent",
                             "filed 240 H-1B petitions since 2024") == (
        "VISA: likely_ok - filed 240 H-1B petitions since 2024"
    )
    blocked = sponsorship.badge("blocked", "frequent", None, ["Must be a U.S. citizen."])
    assert blocked == 'VISA: blocked   - "Must be a U.S. citizen."'
    assert "\n" not in blocked
    unknown = sponsorship.badge("unknown", "none_observed", None)
    assert "no observed H-1B history" in unknown


# --- loading the file -----------------------------------------------------------------------

def test_sponsor_history_loads_and_normalizes_its_keys(tmp_path):
    path = tmp_path / "sponsor_history.yaml"
    path.write_text(textwrap.dedent("""
        acme_corp:
          confidence: frequent
          h1b_filings_recent: 240
    """), encoding="utf-8")
    history = config_loader.load_sponsor_history(path)
    # The key is normalized the same way a posting's company is - underscores become
    # spaces and `normalize_company` drops the corporate suffix - so the script's
    # `acme_corp` and the board's "Acme Corp, Inc." land on one entry rather than two.
    assert list(history) == ["acme"]
    assert sponsorship.confidence_for("Acme Corp, Inc.", history) == "frequent"
    assert sponsorship.confidence_for("Acme", history) == "frequent"


def test_a_missing_sponsor_history_file_is_empty_not_an_error(tmp_path):
    assert config_loader.load_sponsor_history(tmp_path / "nope.yaml") == {}


def test_a_bad_confidence_value_raises_rather_than_scoring_neutral_forever(tmp_path):
    """It feeds a score term. A typo would silently pin the employer at the neutral
    default and never say so."""
    path = tmp_path / "sponsor_history.yaml"
    path.write_text("acme:\n  confidence: often\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="often"):
        config_loader.load_sponsor_history(path)


def test_the_shipped_history_file_loads():
    cfg = config_loader.load()
    assert isinstance(cfg.sponsor_history, dict)
