"""Tests for role classification (spec 7)."""

import pytest

# TODO: from jobmon.filters import role

NEW_GRAD = [
    "Software Engineer, New Grad 2027",
    "University Graduate, Software Engineer",
    "Early Career Software Engineer",
]

INTERNSHIP = [
    "Software Engineer Intern, Summer 2027",
    "MLE Co-op (Spring 2027)",
]

EXPERIENCED = [
    "Senior Software Engineer",
    "Staff Machine Learning Engineer",
    "Engineering Manager, Platform",
]

# Must NOT be misclassified by a naive substring match.
FALSE_FRIENDS = [
    "Internal Tools Software Engineer",      # 'intern' in 'Internal'
    "Software Engineer, International Pay",  # 'intern' in 'International'
    "Principal Investigator Support Engineer",  # 'principal' in a non-seniority sense
]


@pytest.mark.parametrize("title", NEW_GRAD)
def test_new_grad(title):
    pytest.skip("not implemented")


@pytest.mark.parametrize("title", INTERNSHIP)
def test_internship(title):
    pytest.skip("not implemented")


@pytest.mark.parametrize("title", EXPERIENCED)
def test_experienced(title):
    pytest.skip("not implemented")


@pytest.mark.parametrize("title", FALSE_FRIENDS)
def test_word_boundaries(title):
    """TODO: none of these are internships. Substring matching fails all three."""
    pytest.skip("not implemented")


def test_unclear_passes_through():
    """`unclear` is alerted, never dropped (spec 7). A missed posting costs more than a
    false alert. TODO: assert classify_role returns 'unclear', not 'experienced'."""
    pytest.skip("not implemented")


def test_grad_window_extracted_verbatim():
    """TODO: 'must graduate between December 2026 and August 2027' comes back as text, not
    as a parsed date range - the phrasings are too varied to parse safely."""
    pytest.skip("not implemented")
