"""Mute, boost and tune (addendum 6). Testbench T8.11.

T8.11's three clauses, and what each means here:
  * `denylist updated`             -> `Profile.denylist.postings` contains the id
  * `reason recorded`              -> the reason is in state/feedback/<profile>.jsonl
  * `no profile field silently changed` -> the YAML file is byte-identical afterwards
"""

import textwrap

import pytest
from test_scoring import make_analysis, make_posting, policy

from jobmon import feedback, profiles, scoring


@pytest.fixture
def profile_dir(tmp_path):
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "jonathan.yaml").write_text(textwrap.dedent("""
        # a comment worth keeping
        name: jonathan
        roles:
          role_class: [new_grad]
        denylist:
          title_terms: [sdet]
    """), encoding="utf-8")
    return directory


@pytest.fixture
def store_dir(tmp_path):
    return tmp_path / "feedback"


# --- T8.11 ------------------------------------------------------------------------------

def test_t8_11_mute_updates_the_denylist_records_the_reason_and_edits_no_yaml(
    profile_dir, store_dir
):
    """MANDATORY. All three clauses of T8.11, in order."""
    yaml_path = profile_dir / "jonathan.yaml"
    before = yaml_path.read_bytes()

    feedback.record("jonathan", "mute", "gh:acme:1", reason="wrong-function",
                    function="data", company="Acme", directory=store_dir)

    # 1. the denylist a run enforces now contains it
    profile = profiles.load_all(profile_dir, feedback_dir=store_dir)["jonathan"]
    assert "gh:acme:1" in profile.denylist.postings
    assert profile.denylist.title_terms == ["sdet"], "the hand-written list is intact"

    # 2. the reason is recorded
    recorded = feedback.load("jonathan", store_dir)
    assert [(e.kind, e.target, e.reason) for e in recorded] == [
        ("mute", "gh:acme:1", "wrong-function")
    ]

    # 3. no profile field was silently changed - the file was not touched at all
    assert yaml_path.read_bytes() == before
    assert b"a comment worth keeping" in yaml_path.read_bytes()


def test_a_muted_posting_is_never_alerted_again(profile_dir, store_dir):
    posting, analysis = make_posting(), make_analysis()
    profile = policy()
    assert scoring.route(posting, analysis, profile).route != "silent"

    profile.denylist.postings.append(posting.global_id)
    routed = scoring.route(posting, analysis, profile)
    assert routed.route == "silent"
    assert "muted posting" in routed.reasons


def test_mute_company_adds_to_the_company_denylist(profile_dir, store_dir):
    feedback.record("jonathan", "mute_company", "Flood Corp", directory=store_dir)
    profile = profiles.load_all(profile_dir, feedback_dir=store_dir)["jonathan"]
    assert "Flood Corp" in profile.denylist.companies
    routed = scoring.route(make_posting(company="Flood Corp, Inc."), make_analysis(),
                           policy(denylist=profile.denylist))
    assert routed.route == "silent"


def test_boost_raises_the_score_for_that_posting_only(profile_dir, store_dir):
    feedback.record("jonathan", "boost", "gh:acme:1", directory=store_dir)
    profile = profiles.load_all(profile_dir, feedback_dir=store_dir)["jonathan"]
    assert profile.boosted == ["gh:acme:1"]

    boosted = policy()
    boosted.boosted = ["gh:acme:1"]
    plain, _ = scoring.score(make_posting(), make_analysis(), policy())
    lifted, breakdown = scoring.score(make_posting(), make_analysis(), boosted)
    assert lifted > plain
    assert breakdown["boost"] == boosted.scoring.boost_bonus
    other, _ = scoring.score(make_posting(gid="gh:acme:2"),
                             make_analysis(gid="gh:acme:2"), boosted)
    assert other == plain


def test_muting_a_boosted_posting_wins(store_dir):
    """Suppressing something you asked to see is a correction; showing something you asked
    to hide is a bug. The asymmetry is deliberate."""
    feedback.record("p", "boost", "gh:a:1", directory=store_dir)
    feedback.record("p", "mute", "gh:a:1", reason="not-interested", directory=store_dir)
    muted, _, boosted = feedback.for_profile("p", store_dir)
    assert muted == ["gh:a:1"]
    assert boosted == []


def test_muting_twice_produces_one_denylist_entry(store_dir):
    for _ in range(3):
        feedback.record("p", "mute", "gh:a:1", reason="not-interested", directory=store_dir)
    muted, _, _ = feedback.for_profile("p", store_dir)
    assert muted == ["gh:a:1"]


def test_an_unknown_reason_is_rejected(store_dir):
    with pytest.raises(ValueError, match="wrong-planet"):
        feedback.record("p", "mute", "gh:a:1", reason="wrong-planet", directory=store_dir)


def test_a_missing_feedback_file_is_no_feedback(store_dir):
    assert feedback.load("nobody", store_dir) == []
    assert feedback.for_profile("nobody", store_dir) == ([], [], [])


# --- tune -----------------------------------------------------------------------------------

def test_tune_names_the_dominant_reason_and_proposes_a_concrete_edit(store_dir):
    """The addendum's own example: `38 of 51 mutes were wrong-function, 31 of those were
    data. Set functions.data: 0.0?` - rendered here as the ordered-list equivalent."""
    for i in range(8):
        feedback.record("p", "mute", f"gh:a:{i}", reason="wrong-function",
                        function="data", directory=store_dir)
    for i in range(2):
        feedback.record("p", "mute", f"gh:b:{i}", reason="wrong-location",
                        directory=store_dir)

    summary, proposals = feedback.tune("p", store_dir)
    assert "10 mutes recorded" in summary
    assert "wrong-function" in summary
    assert proposals
    assert "8 of 10 mutes were `wrong-function`" in proposals[0].headline
    assert "`data`" in proposals[0].headline
    assert "never: [data]" in proposals[0].edit


def test_tune_proposes_and_never_edits(store_dir, profile_dir):
    """`It proposes. You edit the YAML. No automatic self-tuning: a system that silently
    changes its own filters is one that silently stops showing you jobs.`"""
    before = (profile_dir / "jonathan.yaml").read_bytes()
    for i in range(5):
        feedback.record("jonathan", "mute", f"gh:a:{i}", reason="no-sponsorship",
                        directory=store_dir)
    summary, proposals = feedback.tune("jonathan", store_dir)
    assert "NOT applied" in summary
    assert proposals
    assert (profile_dir / "jonathan.yaml").read_bytes() == before


def test_tune_says_nothing_when_there_is_nothing_to_say(store_dir):
    summary, proposals = feedback.tune("p", store_dir)
    assert "no mutes recorded" in summary
    assert proposals == []


def test_tune_flags_a_company_that_dominates_the_mutes(store_dir):
    for i in range(4):
        feedback.record("p", "mute", f"gh:a:{i}", reason="not-interested",
                        company="Flood Corp", directory=store_dir)
    _, proposals = feedback.tune("p", store_dir)
    assert any("Flood Corp" in p.headline for p in proposals)
    assert any("max_per_company_per_day" in p.edit for p in proposals)
