"""Tests for the dedup rules (spec 4)."""

import pytest

# TODO: from jobmon import store


def test_exact_global_id_updates_last_seen_and_does_not_realert():
    """Primary rule. TODO: second upsert returns 'seen', last_seen_at advances,
    alerted_at is untouched."""
    pytest.skip("not implemented")


def test_fuzzy_match_suppresses_alert_but_records_duplicate():
    """Secondary rule: same (company, normalized_title, first_location) within 60 days.
    Suppress the alert, but WRITE the duplicate row - churn is a signal worth seeing."""
    pytest.skip("not implemented")


def test_fuzzy_window_expires_after_60_days():
    """TODO: the same title 61 days later is a genuinely new posting, not a duplicate."""
    pytest.skip("not implemented")


def test_repost_increments_counter():
    """A posting that disappeared and returned bumps `reposted_count`. Repeatedly reposted
    reqs are often ghost jobs or a hard-to-fill team (spec 4)."""
    pytest.skip("not implemented")


def test_title_edit_does_not_create_a_duplicate():
    """Why content hashing is NOT the dedup key: a company edits the title and a naive hash
    sees a brand new posting. TODO: same global_id, edited title -> 'seen'."""
    pytest.skip("not implemented")
