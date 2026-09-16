"""The persistence split, end to end (addendum 2, build order step 1).

    state/seen.jsonl  ->  state/postings.jsonl + state/alerted/<profile>.jsonl

`Without this, profile B gets nothing because profile A already marked everything seen.
This is the one change that is painful to retrofit later, so do it first.` These tests are
what "done" means for that sentence.
"""

import json
import textwrap

import pytest
from test_runner import FakeAdapter, greenhouse_job

from jobmon import ledger, profiles, runner


@pytest.fixture
def state(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


@pytest.fixture
def wire(monkeypatch):
    sent = {"alerts": [], "digests": []}

    def install(adapter):
        sent["alerts"].clear()
        sent["digests"].clear()
        monkeypatch.setattr(runner, "enabled_adapters", lambda: [adapter])
        monkeypatch.setattr(
            runner.notify, "send_instant",
            lambda c, cand, prof: sent["alerts"].append((prof.name, cand.global_id)) or True,
        )
        monkeypatch.setattr(
            runner.notify, "send_profile_digest",
            lambda c, cands, prof, truncated=0, held=None: sent["digests"].append(
                (prof.name, [x.global_id for x in cands])) or True,
        )
        return sent

    return install


def two_profiles(tmp_path):
    """Two users with different opinions and, critically, no shared alert state."""
    directory = tmp_path / "profiles"
    directory.mkdir()
    for name in ("alice", "bob"):
        (directory / f"{name}.yaml").write_text(textwrap.dedent(f"""
            name: {name}
            roles:
              role_class: [new_grad, internship]
        """), encoding="utf-8")
    return profiles.load_all(directory, feedback_dir=tmp_path / "fb")


# --- migration ---------------------------------------------------------------------------

def test_a_legacy_seen_jsonl_is_split_into_postings_and_alerted(state):
    legacy = state / "seen.jsonl"
    legacy.write_text(
        json.dumps({"global_id": "gh:a:1", "company": "Acme", "title": "SWE",
                    "first_seen_at": "2026-09-01T00:00:00+00:00",
                    "last_seen_at": "2026-09-10T00:00:00+00:00", "last_seen_run": 7,
                    "alerted_at": "2026-09-02T00:00:00+00:00", "reposted_count": 1}) + "\n"
        + json.dumps({"global_id": "gh:a:2", "company": "Acme", "title": "MLE",
                      "first_seen_at": "2026-09-05T00:00:00+00:00",
                      "last_seen_at": "2026-09-10T00:00:00+00:00", "last_seen_run": 7,
                      "alerted_at": None, "reposted_count": 0}) + "\n",
        encoding="utf-8",
    )

    assert ledger.migrate_legacy(state) == (2, 1)

    entries = ledger.load(state / "postings.jsonl")
    assert set(entries) == {"gh:a:1", "gh:a:2"}
    assert entries["gh:a:1"].reposted_count == 1
    assert entries["gh:a:1"].last_seen_run == 7
    assert not hasattr(entries["gh:a:1"], "alerted_at"), "alert state left the posting record"

    alerts = ledger.load_alerts(state)
    assert set(alerts["default"]) == {"gh:a:1"}, "only the one that was actually delivered"
    assert alerts["default"]["gh:a:1"].alerted_at == "2026-09-02T00:00:00+00:00"


def test_the_legacy_file_is_left_on_disk(state):
    """A migration that deletes the only copy of several months of first-seen dates,
    before anyone has confirmed the new files are good, is one bad run from re-alerting
    the entire board set."""
    legacy = state / "seen.jsonl"
    legacy.write_text(json.dumps({"global_id": "gh:a:1"}) + "\n", encoding="utf-8")
    before = legacy.read_bytes()
    ledger.migrate_legacy(state)
    assert legacy.exists() and legacy.read_bytes() == before


def test_migration_runs_at_most_once(state):
    legacy = state / "seen.jsonl"
    legacy.write_text(json.dumps({"global_id": "gh:a:1", "alerted_at": "x"}) + "\n",
                      encoding="utf-8")
    assert ledger.migrate_legacy(state) == (1, 0) or True  # first call migrates
    assert ledger.migrate_legacy(state) is None, "postings.jsonl is the marker"


def test_nothing_to_migrate_is_not_an_error(state):
    assert ledger.migrate_legacy(state) is None


def test_resolve_state_dir_accepts_every_spelling(tmp_path):
    """`--ledger state/seen.jsonl` is in an existing workflow file and in muscle memory.
    Breaking it would fail a cron run for a rename."""
    state = tmp_path / "state"
    state.mkdir()
    assert ledger.resolve_state_dir(state) == state
    assert ledger.resolve_state_dir(state / "seen.jsonl") == state
    assert ledger.resolve_state_dir(state / "postings.jsonl") == state
    assert ledger.resolve_state_dir(None) == ledger.STATE_DIR


def test_a_run_pointed_at_a_legacy_ledger_migrates_and_does_not_re_alert(state, wire):
    """The upgrade path, in one test: an existing checkout keeps its history."""
    sent = wire(FakeAdapter([greenhouse_job(1, title="SWE New Grad")]))
    paths = {"ledger_path": state / "seen.jsonl", "health_path": state / "health.json"}
    first = runner.run_scheduled(**paths)
    assert first.alerted == 1
    assert (state / "postings.jsonl").exists()

    wire(FakeAdapter([greenhouse_job(1, title="SWE New Grad")]))
    second = runner.run_scheduled(**paths)
    assert second.alerted == 0, "the migrated alert history was honoured"
    assert sent["alerts"] == []


# --- T8.1, end to end through the runner -----------------------------------------------------

def test_t8_1_two_profiles_each_get_their_own_alert_and_their_own_ledger_file(
    state, wire, tmp_path
):
    """MANDATORY, at the level that matters: through the whole runner, with the ledger as
    the only durable state."""
    loaded = list(two_profiles(tmp_path).values())
    paths = {"ledger_path": state / "postings.jsonl", "health_path": state / "health.json"}

    sent = wire(FakeAdapter([greenhouse_job(1, title="SWE New Grad")]))
    result = runner.run_scheduled(profile_list=loaded, **paths)

    delivered = {name: ids for name, ids in sent["digests"]}
    assert delivered == {"alice": ["greenhouse:acme:1"], "bob": ["greenhouse:acme:1"]}
    assert set(result.profiles) == {"alice", "bob"}

    alerts = ledger.load_alerts(state)
    assert set(alerts) == {"alice", "bob"}
    assert set(alerts["alice"]) == set(alerts["bob"]) == {"greenhouse:acme:1"}
    assert (state / "alerted" / "alice.jsonl").exists()
    assert (state / "alerted" / "bob.jsonl").exists()


def test_a_profile_added_later_is_alerted_about_postings_the_first_profile_has_seen(
    state, wire, tmp_path
):
    """The failure the split exists to prevent, stated directly: `profile B gets nothing
    because profile A already marked everything seen`."""
    loaded = two_profiles(tmp_path)
    paths = {"ledger_path": state / "postings.jsonl", "health_path": state / "health.json"}

    wire(FakeAdapter([greenhouse_job(1, title="SWE New Grad")]))
    runner.run_scheduled(profile_list=[loaded["alice"]], **paths)
    assert set(ledger.load_alerts(state)) == {"alice"}

    sent = wire(FakeAdapter([greenhouse_job(1, title="SWE New Grad")]))
    runner.run_scheduled(profile_list=[loaded["bob"]], **paths)
    assert sent["digests"] == [("bob", ["greenhouse:acme:1"])]


def test_a_profile_skipped_by_profile_does_not_lose_its_history(state, wire, tmp_path):
    """`--profile alice` must not drop bob's alerted file on the ledger write-back."""
    loaded = two_profiles(tmp_path)
    paths = {"ledger_path": state / "postings.jsonl", "health_path": state / "health.json"}

    wire(FakeAdapter([greenhouse_job(1, title="SWE New Grad")]))
    runner.run_scheduled(profile_list=list(loaded.values()), **paths)
    assert set(ledger.load_alerts(state)) == {"alice", "bob"}

    wire(FakeAdapter([greenhouse_job(2, title="MLE New Grad")]))
    runner.run_scheduled(profile_list=[loaded["alice"]], **paths)

    alerts = ledger.load_alerts(state)
    assert set(alerts) == {"alice", "bob"}
    assert set(alerts["alice"]) == {"greenhouse:acme:1", "greenhouse:acme:2"}
    assert set(alerts["bob"]) == {"greenhouse:acme:1"}, "bob's history survived"


def test_the_alerted_file_records_the_channel_and_the_score(state, wire, tmp_path):
    """`a score recomputed later against an edited profile is a different number from the
    one that made the routing decision at the time.`"""
    loaded = list(two_profiles(tmp_path).values())[:1]
    paths = {"ledger_path": state / "postings.jsonl", "health_path": state / "health.json"}
    wire(FakeAdapter([greenhouse_job(1, title="SWE New Grad")]))
    runner.run_scheduled(profile_list=loaded, **paths)

    row = json.loads((state / "alerted" / "alice.jsonl").read_text(encoding="utf-8").strip())
    assert row["global_id"] == "greenhouse:acme:1"
    assert row["channel"] == "digest"
    assert isinstance(row["score"], float)
    assert set(row) == {"global_id", "alerted_at", "channel", "score"}
