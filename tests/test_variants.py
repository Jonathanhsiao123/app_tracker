"""Testbench T0 - variant scoring is decoupled from the resume PDFs (build step 0),
plus the confidence gate on the recommendation.

The gate: scoring runs with `pdf: null` everywhere. Nothing in this file may open a PDF.
Profiles come from `Config.variants`; `variants.py` no longer reads YAML itself.
"""

import logging
import textwrap

import pytest

from jobmon import config_loader
from jobmon.analyze import variants

ADVERSARIAL_ML = {  # T5.7 fixture
    "ml_topics": ["adversarial", "robustness"],
    "ml_frameworks": ["pytorch"],
    "languages": ["python"],
}
FIRMWARE = {  # T5.8 fixture
    "systems": ["firmware", "embedded", "instrumentation"],
    "languages": ["c", "python"],
}
# One term in every category: the posting that points nowhere in particular.
GENERIC = {
    "languages": ["python"],
    "ml_frameworks": ["pytorch"],
    "ml_topics": ["llm"],
    "infra": ["aws"],
    "systems": ["embedded"],
    "security": ["iot"],
}


@pytest.fixture
def profiles():
    return config_loader.load().variants


def write_variants(tmp_path, body):
    path = tmp_path / "variants.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --- T0.1: all four `pdf: null` -> scoring runs, no exception ----------------------

def test_shipped_config_has_all_four_variants_with_no_pdf(profiles):
    assert sorted(profiles) == ["generalist", "mle", "security", "systems"]
    assert all(p.pdf is None for p in profiles.values())


def test_scoring_runs_with_every_pdf_null(profiles):
    scores = variants.score(ADVERSARIAL_ML, profiles)
    assert set(scores) == set(profiles)
    assert all(isinstance(v, float) for v in scores.values())
    assert variants.recommend(scores) in profiles


def test_pdf_note_marks_the_missing_document_without_blocking(profiles):
    assert variants.pdf_note(profiles["mle"]) == "mle (PDF not yet written)"


def test_no_matched_terms_scores_zero_and_falls_back_to_generalist(profiles):
    """A posting matching nothing must not ZeroDivisionError."""
    scores = variants.score({}, profiles)
    assert set(scores.values()) == {0.0}
    assert variants.recommend(scores) == "generalist"
    assert variants.is_confident(scores)[0] is False


# --- T0.2: a `pdf` path that is set but missing -> warning, scoring still runs -----

def test_missing_pdf_file_warns_but_still_loads(tmp_path, caplog):
    path = write_variants(
        tmp_path,
        """
        mle:
          pdf: resumes/does_not_exist.pdf
          weights: {ml_topics: 1.0}
        """,
    )
    with caplog.at_level(logging.WARNING, logger=config_loader.__name__):
        loaded = config_loader.load_variants(path)
    assert "does_not_exist.pdf" in caplog.text
    assert variants.score(ADVERSARIAL_ML, loaded)["mle"] == pytest.approx(0.5)


# --- T0.3: a weights block missing a category -> contributes 0, no KeyError --------

def test_category_absent_from_weights_contributes_zero(tmp_path):
    path = write_variants(tmp_path, "narrow:\n  pdf: null\n  weights: {ml_topics: 1.0}\n")
    loaded = config_loader.load_variants(path)
    # ml_frameworks and languages are unweighted here: 2 matched ml_topics out of 4 terms.
    assert variants.score(ADVERSARIAL_ML, loaded)["narrow"] == pytest.approx(0.5)


def test_empty_weights_block_is_legal(tmp_path):
    path = write_variants(tmp_path, "bare:\n  pdf: null\n")
    loaded = config_loader.load_variants(path)
    assert variants.score(ADVERSARIAL_ML, loaded)["bare"] == 0.0


def test_requirements_category_never_influences_scoring(profiles):
    """`requirements` (ms required / phd preferred) is a flag, not a variant signal.
    No variant weights it, so T0.3's default-to-zero keeps it out with no special case."""
    plain = variants.score(ADVERSARIAL_ML, profiles)
    with_reqs = variants.score(ADVERSARIAL_ML | {"requirements": ["phd required"]}, profiles)
    assert variants.recommend(plain) == variants.recommend(with_reqs)
    assert all(p.weights.get("requirements", 0.0) == 0.0 for p in profiles.values())


def test_unknown_taxonomy_category_warns_when_taxonomy_is_supplied(tmp_path, caplog):
    path = write_variants(tmp_path, "typo:\n  pdf: null\n  weights: {ml_topic: 1.0}\n")
    with caplog.at_level(logging.WARNING, logger=config_loader.__name__):
        config_loader.load_variants(path, taxonomy={"ml_topics": ["adversarial"]})
    assert "ml_topic" in caplog.text


def test_shipped_config_categories_all_exist_in_taxonomy(caplog):
    with caplog.at_level(logging.WARNING, logger=config_loader.__name__):
        config_loader.load()
    assert caplog.text == ""


# --- confidence gate: threshold 0.70 AND lead 0.10 --------------------------------

def test_clear_ml_posting_is_confident(profiles):
    scores = variants.score(ADVERSARIAL_ML, profiles)
    assert variants.recommend(scores) == "mle"
    assert min(scores, key=lambda n: scores[n]) == "systems"  # T5.7
    confident, reason = variants.is_confident(scores)
    assert confident is True, reason


def test_clear_firmware_posting_is_confident(profiles):
    scores = variants.score(FIRMWARE, profiles)
    assert variants.recommend(scores) == "systems"  # T5.8
    assert variants.is_confident(scores)[0] is True


def test_generic_posting_is_not_confident_despite_mle_winning(profiles):
    """The weight-total bias: mle wins a posting with no ML signal, because its weights
    block covers the most categories. The gate catches it; the weights are still wrong."""
    scores = variants.score(GENERIC, profiles)
    assert variants.recommend(scores) == "mle"
    confident, reason = variants.is_confident(scores)
    assert confident is False
    assert "below" in reason


def test_high_score_but_thin_lead_is_not_confident():
    scores = {"mle": 0.95, "security": 0.90, "generalist": 0.10, "systems": 0.05}
    confident, reason = variants.is_confident(scores)
    assert confident is False
    assert "lead" in reason


def test_wide_lead_but_low_score_is_not_confident():
    scores = {"mle": 0.40, "security": 0.05, "generalist": 0.02, "systems": 0.0}
    confident, reason = variants.is_confident(scores)
    assert confident is False
    assert "top score" in reason


def test_lead_is_the_gap_between_the_top_two():
    assert variants.lead({"a": 0.9, "b": 0.7, "c": 0.1}) == pytest.approx(0.2)
    assert variants.lead({"a": 0.9}) == 0.0
    assert variants.lead({}) == 0.0


def test_confidence_gate_never_suppresses_an_alert():
    """`is_confident` gates the RECOMMENDATION only. notify.should_alert lists the four
    suppression rules and variant score is not among them."""
    import inspect

    from jobmon import notify

    doc = inspect.getdoc(notify.should_alert)
    assert "variant" not in doc.lower()
