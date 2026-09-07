"""Testbench T4 - eligibility classification (spec 6, build step 4).

The gate: **"authorized to work in the US" is never `blocked`.** A false `blocked` is a
posting you never see, so MUST_NOT_BLOCK is the half of this file that matters.
"""

import re

import pytest

from jobmon.filters import eligibility as e

# --- T4: MUST_NOT_BLOCK - the case that matters ------------------------------------

MUST_NOT_BLOCK = [
    "Must be legally authorized to work in the United States.",
    "Applicants must have authorization to work in the US.",
    "Candidates must be authorized to work in the country of employment.",
    "We are an equal opportunity employer.",
    "This role requires a background check.",
    "You must be eligible to work in the United States without restriction.",
    "All applicants must be legally authorized to work in the U.S. at the time of hire.",
]


@pytest.mark.parametrize("sentence", MUST_NOT_BLOCK)
def test_must_not_block(sentence):
    label, evidence = e.classify(sentence, "Acme")
    assert label != "blocked", f"{sentence!r} -> {label} on {evidence}"


@pytest.mark.parametrize("sentence", MUST_NOT_BLOCK)
def test_no_blocking_pattern_matches_a_work_authorization_sentence(sentence):
    """Belt and braces: not just the label, but that no blocking regex fires at all."""
    hits = [p for p in e.BLOCKING_PATTERNS if re.search(p, sentence, re.IGNORECASE)]
    assert hits == [], f"{sentence!r} matched {hits}"


def test_sponsorship_available_is_likely_ok():
    label, evidence = e.classify("Visa sponsorship is available for this role.", "Acme")
    assert label == "likely_ok"
    assert evidence == ["Visa sponsorship is available for this role."]


def test_f1_cpt_opt_welcome_is_likely_ok():
    label, _ = e.classify("We welcome F-1 students on CPT or OPT.", "Acme")
    assert label == "likely_ok"


def test_no_relevant_language_is_unknown():
    label, evidence = e.classify(
        "We build distributed systems in Rust. You will own a service end to end.", "Acme"
    )
    assert label == "unknown"
    assert evidence == []


def test_empty_description_is_unknown():
    """Curated-repo rows have no body at all; they must not be blocked by absence."""
    assert e.classify("", "Acme") == ("unknown", [])


# --- T4b: MUST_BLOCK ---------------------------------------------------------------

MUST_BLOCK = [
    "Must be a U.S. citizen.",
    "Requires an active TS/SCI clearance.",
    "Must qualify as a U.S. Person under ITAR.",
    "We are unable to sponsor or take over sponsorship of an employment visa.",
    "This position does not offer visa sponsorship.",
    "Must have permanent work authorization without the need for sponsorship.",
    "U.S. citizenship is required for this position.",
    "This role requires the ability to obtain a security clearance.",
    "We will not sponsor applicants for work visas.",
]


@pytest.mark.parametrize("sentence", MUST_BLOCK)
def test_must_block(sentence):
    label, evidence = e.classify(sentence, "Acme")
    assert label == "blocked", f"{sentence!r} -> {label}"
    assert evidence and evidence[0] == sentence  # verbatim


def test_the_permanent_work_authorization_case_blocks_despite_the_guard_words():
    """This sentence contains "work authorization" - a NEVER_BLOCK_ON phrase - and still
    blocks, because the blocking pattern keys on "without the need for sponsorship"."""
    sentence = "Must have permanent work authorization without the need for sponsorship."
    assert e.classify(sentence, "Acme")[0] == "blocked"


# --- T4c: mechanics ----------------------------------------------------------------

def test_blocking_and_positive_in_one_posting_blocks_and_keeps_both():
    text = ("Visa sponsorship is available for most roles. "
            "This particular position requires an active security clearance.")
    label, evidence = e.classify(text, "Acme")
    assert label == "blocked"
    assert len(evidence) == 2
    assert any("clearance" in s for s in evidence)
    assert any("sponsorship is available" in s for s in evidence)


def test_a_blocking_phrase_spanning_a_sentence_boundary_is_not_matched():
    """Matching is sentence-scoped by design. A phrase straddling a boundary is a
    coincidence, and a false `blocked` costs a posting you never see."""
    text = "We are unable to. Sponsor applications close on Friday."
    assert e.classify(text, "Acme")[0] != "blocked"


def test_bullet_lines_are_separate_sentences():
    """The HTML stripper turns bullets into newline-prefixed lines with no terminal
    period; splitting on punctuation alone would run two bullets together."""
    text = "- We are an equal opportunity employer\n- Must be a U.S. citizen"
    label, evidence = e.classify(text, "Acme")
    assert label == "blocked"
    assert evidence == ["- Must be a U.S. citizen"]


def test_evidence_is_verbatim_not_paraphrased():
    sentence = "Applicants must qualify as a U.S. Person as defined by ITAR."
    _, evidence = e.classify(f"We build rockets. {sentence} Apply now.", "Acme")
    assert sentence in evidence


# --- overrides ---------------------------------------------------------------------

OVERRIDES = {
    "anduril": {"status": "blocked", "note": "Defense prime; ITAR-gated.",
                "checked_on": "2026-09-03"},
    "stripe": {"status": "likely_ok", "note": "Consistent H-1B filings.",
               "checked_on": "2026-09-03"},
}


def test_override_beats_text_classification():
    """T4c: overrides win. Text says blocked, the override says likely_ok."""
    text = "We are unable to sponsor employment visas."
    label, evidence = e.classify(text, "Stripe", OVERRIDES)
    assert label == "likely_ok"
    assert "override for stripe" in evidence[0]


def test_override_evidence_notes_the_override_and_keeps_the_text():
    text = "We are unable to sponsor employment visas."
    _, evidence = e.classify(text, "Stripe", OVERRIDES)
    assert "likely_ok" in evidence[0] and "checked 2026-09-03" in evidence[0]
    assert text in evidence  # the contradicting text is still shown


def test_override_can_block_a_posting_with_no_blocking_text():
    label, evidence = e.classify("We build autonomous systems.", "Anduril", OVERRIDES)
    assert label == "blocked"
    assert "ITAR-gated" in evidence[0]


def test_override_matches_across_legal_suffixes():
    '''An override keyed "anduril" must match "Anduril Industries, Inc.".'''
    label, _ = e.classify("We build things.", "Anduril Industries, Inc.", OVERRIDES)
    assert label == "blocked"


def test_empty_overrides_file_does_not_break_the_code_path():
    """The file is empty now; the code path must work anyway (build step 4.3)."""
    assert e.classify("We build things.", "Acme", {}) == ("unknown", [])
    assert e.classify("We build things.", "Acme", None) == ("unknown", [])


def test_override_with_an_invalid_status_is_ignored_not_obeyed():
    bad = {"acme": {"status": "maybe", "note": "n", "checked_on": "2026-01-01"}}
    assert e.apply_override("Acme", bad) is None


def test_shipped_overrides_file_loads_and_is_wired():
    from jobmon import config_loader

    cfg = config_loader.load()
    assert isinstance(cfg.sponsorship_overrides, dict)  # empty today, must still work
    label, _ = e.classify("We build things.", "Acme", cfg.sponsorship_overrides)
    assert label == "unknown"


# --- the permanent guard (T4c) -----------------------------------------------------

def test_never_block_on_terms_are_absent_from_every_blocking_list():
    """PERMANENT. Adding a NEVER_BLOCK_ON term to a blocking list must fail loudly here,
    not silently cost postings. "authorized to work" is not a block: F-1 with CPT/OPT
    *is* authorized (spec 6)."""
    for guard in e.NEVER_BLOCK_ON:
        for pattern in e.BLOCKING_PATTERNS:
            assert guard not in pattern, f"{guard!r} leaked into blocking pattern {pattern!r}"


def test_every_never_block_phrase_classifies_as_unknown_on_its_own():
    for guard in e.NEVER_BLOCK_ON:
        phrase = guard.replace("\\", "")
        assert e.classify(f"Candidates must be {phrase} in the US.", "Acme")[0] != "blocked"


# --- acronym case sensitivity ------------------------------------------------------

def test_lowercase_ear_is_not_an_export_control_block():
    """`\\bEAR\\b` under IGNORECASE matches the ordinary word "ear"."""
    assert e.classify("You will design in-ear audio products. ear", "Acme")[0] != "blocked"


def test_uppercase_EAR_does_block():
    assert e.classify("This role is subject to EAR restrictions.", "Acme")[0] == "blocked"


def test_lowercase_opt_is_not_a_positive_signal():
    """"opt" appears constantly ("opt-in", "you may opt to")."""
    assert e.classify("You can opt to work remotely two days a week.", "Acme")[0] == "unknown"


# --- grouping, for the audit report ------------------------------------------------

@pytest.mark.parametrize(
    "sentence,group",
    [
        ("Must be a U.S. citizen.", "citizenship"),
        ("Requires an active TS/SCI clearance.", "clearance"),
        # "U.S. Person under ITAR" matches both citizenship and export_control; the first
        # group in BLOCKING_GROUPS wins, which is fine - the audit report only needs a
        # family label, and the evidence sentence carries the detail.
        ("Must qualify as a U.S. Person under ITAR.", "citizenship"),
        ("This role is subject to ITAR restrictions.", "export_control"),
        ("We are unable to sponsor employment visas.", "sponsorship_refusal"),
    ],
)
def test_blocking_group_labels_the_signal_family(sentence, group):
    assert e.blocking_group(sentence) == group


def test_blocking_group_is_none_for_a_clean_sentence():
    assert e.blocking_group("We build distributed systems.") is None
