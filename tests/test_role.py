"""Testbench T3b - role classification and word boundaries (spec 7, build step 3).

`unclear` is alerted, never dropped: a missed posting costs more than a false alert.
"""

import pytest

from jobmon.filters import role

GRAD_BODY = "Candidates must graduate between December 2026 and August 2027."


# --- T3b: the table, verbatim ------------------------------------------------------

@pytest.mark.parametrize(
    "title,body,expected",
    [
        ("Internal Tools Engineer", "", "unclear"),
        ("International Payments SWE", "", "unclear"),
        ("Software Engineer Intern, Summer 2027", "", "internship"),
        ("Software Engineer II", "Requires 3+ years of experience.", "experienced"),
        ("Software Engineer, University Graduate", "", "new_grad"),
        ("Software Engineer", GRAD_BODY, "new_grad"),
        ("Senior Software Engineer, New Grad Program", "", "experienced"),
        ("Software Engineer", "", "unclear"),
    ],
)
def test_t3b_table(title, body, expected):
    assert role.classify_role(title, body) == expected


def test_grad_window_populated_for_the_unmarked_title():
    window = role.extract_grad_window(GRAD_BODY)
    assert window == GRAD_BODY  # verbatim, not a parsed range


# --- word boundaries: the false friends --------------------------------------------

@pytest.mark.parametrize(
    "title",
    [
        "Internal Tools Software Engineer",
        "Software Engineer, International Pay",
        "Internationalization Engineer",
    ],
)
def test_intern_substring_does_not_fire(title):
    assert role.classify_role(title, "") != "internship"


def test_principal_investigator_is_still_seniority_in_a_title():
    """"Principal Investigator Support Engineer" reads as seniority to this filter and is
    rejected. A real miss, and the safe direction is not obvious: loosening it would let
    "Principal Engineer" through, which is a far more common title."""
    assert role.classify_role("Principal Investigator Support Engineer", "") == "experienced"


def test_seniority_in_the_body_does_not_reject():
    """Almost every posting says a senior engineer will mentor you."""
    body = "You will be mentored by senior engineers and our staff architects."
    assert role.classify_role("Software Engineer, New Grad", body) == "new_grad"


# --- II/III only rejects with a YoE floor above 2 ----------------------------------

def test_level_two_without_a_yoe_floor_is_not_rejected():
    """At some companies "Software Engineer II" IS the new grad level."""
    assert role.classify_role("Software Engineer II", "") == "unclear"


def test_level_two_with_a_low_floor_is_not_rejected():
    assert role.classify_role("Software Engineer II", "1+ years of experience.") == "unclear"


# --- YoE extraction ----------------------------------------------------------------

@pytest.mark.parametrize(
    "body,expected",
    [
        ("5+ years of experience required.", 5),
        ("Minimum of 5 years of experience.", 5),
        ("We require 3-5 years of experience.", 3),
        ("2+ years required, 5+ years preferred.", 2),  # the floor, not the ceiling
        ("No experience required.", None),
        ("A 4-year degree in computer science.", None),
        ("", None),
    ],
)
def test_extract_yoe_floor(body, expected):
    assert role.extract_yoe_floor(body) == expected


# --- grad window -------------------------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [
        "Must graduate between December 2026 and August 2027.",
        "Expected graduation date between Dec 2026 and Jun 2027.",
        "Open to students graduating in 2027.",
        "Degree conferral no later than June 2027.",
    ],
)
def test_grad_window_variants_are_found(body):
    assert role.extract_grad_window(body) is not None


def test_no_grad_window_returns_none():
    assert role.extract_grad_window("We build distributed systems in Rust.") is None


# --- unclear passes through --------------------------------------------------------

def test_unclear_is_not_experienced():
    """`unclear` is alerted; `experienced` is suppressed. Confusing them drops real jobs."""
    assert role.classify_role("Software Engineer", "We build things.") == "unclear"


# --- function classification -------------------------------------------------------

def test_function_prefers_the_title():
    fns = role.classify_function("Machine Learning Engineer, New Grad", "We use Python.")
    assert "mle" in fns


def test_incidental_ml_mention_does_not_make_it_mle():
    body = "Our platform serves machine learning teams."
    assert "mle" not in role.classify_function("Software Engineer", body)


def test_function_returns_empty_rather_than_defaulting_to_swe():
    assert role.classify_function("Barista", "Make coffee.") == []


def test_a_posting_can_carry_two_functions():
    fns = role.classify_function(
        "Security Software Engineer", "Threat modeling for our backend security platform."
    )
    assert "security" in fns and "swe" in fns


# --- degree gating -----------------------------------------------------------------

@pytest.mark.parametrize(
    "body,gated",
    [
        ("MS or PhD required.", True),
        ("PhD required in a related field.", True),
        ("MS preferred.", False),
        ("PhD preferred, but not required.", False),
        ("Bachelor's degree in Computer Science.", False),
        ("", False),
    ],
)
def test_is_degree_gated(body, gated):
    assert role.is_degree_gated(body) is gated
