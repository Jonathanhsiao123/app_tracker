"""Tests for the eligibility classifier (spec 6).

This filter gets its own test file before anything else does, because its failure mode is
invisible: a false `blocked` suppresses an alert, and a suppressed alert is a posting that
was never seen. The cases below are the ones the spec calls out by name.
"""

import pytest

# TODO: from jobmon.filters import eligibility

BLOCKS = [
    "Applicants must be a US citizen.",
    "This position requires an active security clearance (TS/SCI).",
    "Due to ITAR requirements, candidates must be a US Person.",
    "We are unable to sponsor or take over sponsorship of an employment visa.",
    "Candidates must have permanent work authorization without the need for sponsorship.",
]

# The trap. F-1 with CPT/OPT IS authorized to work. None of these may block.
MUST_NOT_BLOCK = [
    "Must be authorized to work in the United States.",
    "You must be legally authorized to work in the US at the time of hire.",
    "Applicants must have work authorization in the country of employment.",
]

POSITIVE = [
    "Visa sponsorship is available for this role.",
    "We sponsor H-1B and welcome F-1 students.",
    "CPT and OPT eligible.",
]

UNKNOWN = [
    "We are an equal opportunity employer.",
    "Requirements: proficiency in Python and a passion for distributed systems.",
]


@pytest.mark.parametrize("text", BLOCKS)
def test_blocking_signals(text):
    """TODO: assert label == 'blocked' AND that the evidence sentence is returned verbatim."""
    pytest.skip("not implemented")


@pytest.mark.parametrize("text", MUST_NOT_BLOCK)
def test_generic_authorization_never_blocks(text):
    """The single most important test in the suite (spec 6). A generic 'authorized to work'
    is not a block. TODO: assert label != 'blocked'."""
    pytest.skip("not implemented")


@pytest.mark.parametrize("text", POSITIVE)
def test_positive_signals(text):
    """TODO: assert label == 'likely_ok'."""
    pytest.skip("not implemented")


@pytest.mark.parametrize("text", UNKNOWN)
def test_unknown_is_the_default(text):
    """`unknown` is the MAJORITY case and must alert normally. TODO: assert 'unknown'."""
    pytest.skip("not implemented")


def test_override_beats_text():
    """A company override wins over any textual signal (spec 6).
    TODO: text says 'unable to sponsor', override says likely_ok -> likely_ok, and the
    override note plus checked_on date appear in the evidence."""
    pytest.skip("not implemented")
