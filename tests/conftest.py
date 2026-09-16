"""Shared fixtures.

ISOLATION FROM THE SHIPPED USER CONFIG. Since the addendum, `profiles.select()` reads
`jobmon/config/profiles/` and `feedback.FEEDBACK_DIR` reads `state/feedback/`. Both are one
real user's opinions, and a test suite that silently depends on them is a suite that breaks
when he reorders `priorities.order`. `isolate_profiles` points both at empty tmp
directories, so every test that does not say otherwise runs against the BUILT-IN DEFAULT
profile - which is deliberately the pre-addendum behaviour: unclear roles alert, unknown
eligibility alerts, no caps.

A test that wants a specific profile builds one (`profiles.Profile(...)`) or writes a YAML
file and points `profiles.PROFILES_DIR` at it. `tests/test_profiles.py` and
`tests/test_scoring.py` do exactly that.
"""

import pytest

from jobmon import feedback, profiles


@pytest.fixture(autouse=True)
def isolate_profiles(tmp_path, monkeypatch):
    """Run against the built-in default profile and an empty feedback store."""
    empty = tmp_path / "no-profiles"
    empty.mkdir()
    monkeypatch.setattr(profiles, "PROFILES_DIR", empty)
    monkeypatch.setattr(feedback, "FEEDBACK_DIR", tmp_path / "feedback")
    return empty
