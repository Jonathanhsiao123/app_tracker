"""Testbench T6 - committed ledger and the zero-result canary (build step 6).

Mandatory gates: T6.1 (three runs, ledger as the only state), T6.3 (a failed notify leaves
the ledger byte-identical), T6.5 (the canary fires and the pipeline alert is distinct).
Cron goes on only after these pass.

No network: the adapter and the notifier are replaced throughout.
"""

import json

import pytest
from test_runner import FakeAdapter, greenhouse_job  # same dir; tests is not a package

from jobmon import ledger, runner


@pytest.fixture
def paths(tmp_path):
    """The split ledger (addendum 2, build step 1): postings and alert state are two files.

    `ledger_path` still names one file because `--ledger` and monitor.yml do;
    `ledger.resolve_state_dir` turns it into the directory both files live in.
    """
    return {"ledger_path": tmp_path / "state" / "postings.jsonl",
            "health_path": tmp_path / "state" / "health.json"}


def alerts_file(paths, profile="default"):
    return ledger.alerted_path(paths["ledger_path"].parent, profile)


def alerted_ids(paths, profile="default"):
    """global_ids this profile has been told about, read back off disk."""
    return set(ledger.load_alerts(paths["ledger_path"].parent).get(profile, {}))


@pytest.fixture
def wire(monkeypatch):
    """Fake adapter + captured notifier. Each call resets the capture."""
    sent = {"alerts": [], "digests": [], "pipeline": []}

    def install(adapter, alert_ok=True, digest_ok=True, notify_raises=None):
        sent["alerts"].clear(); sent["digests"].clear(); sent["pipeline"].clear()
        monkeypatch.setattr(runner, "enabled_adapters", lambda: [adapter])

        def _alert(c, p, a):
            if notify_raises:
                raise notify_raises
            sent["alerts"].append(p.global_id)
            return alert_ok

        def _digest(c, items, label):
            if notify_raises:
                raise notify_raises
            sent["digests"].append([p.global_id for p, _ in items])
            return digest_ok

        monkeypatch.setattr(
            runner.notify, "send_instant",
            lambda c, cand, prof: _alert(c, cand.posting, cand.analysis),
        )
        monkeypatch.setattr(
            runner.notify, "send_profile_digest",
            lambda c, cands, prof, truncated=0, held=None, **kw: _digest(
                c, [(x.posting, x.analysis) for x in cands], "digest"),
        )
        return sent

    return install


def jobs(n):
    return [greenhouse_job(i, title=f"SWE New Grad {chr(65 + i)}") for i in range(n)]


# --- T6.1: three runs, ledger as the only persisted state --------------------------

def test_t6_1_run1_alerts_n_runs_2_and_3_alert_zero(paths, wire):
    """THE GATE. Each run gets a fresh temp cache; only the ledger carries over."""
    wire(FakeAdapter(jobs(4)))
    first = runner.run_scheduled(**paths)
    assert first.new == 4 and first.alerted == 4

    wire(FakeAdapter(jobs(4)))
    second = runner.run_scheduled(**paths)
    assert second.alerted == 0

    wire(FakeAdapter(jobs(4)))
    third = runner.run_scheduled(**paths)
    assert third.alerted == 0


def test_ledger_is_written_and_reloadable(paths, wire):
    wire(FakeAdapter(jobs(3)))
    runner.run_scheduled(**paths)
    entries = ledger.load(paths["ledger_path"])
    assert len(entries) == 3
    # Alert state is no longer a field on the posting entry - it is a separate file, one
    # per profile, which is the whole point of the split.
    assert alerted_ids(paths) == set(entries)


# --- T6.2: ledger deleted -> re-alerts everything ----------------------------------

def test_t6_2_deleting_the_ledger_re_alerts_everything(paths, wire):
    """Confirms the ledger is the SOLE source of truth. If anything else were persisting
    dedup state, this would alert 0."""
    wire(FakeAdapter(jobs(3)))
    assert runner.run_scheduled(**paths).alerted == 3
    paths["ledger_path"].unlink()
    alerts_file(paths).unlink()
    wire(FakeAdapter(jobs(3)))
    assert runner.run_scheduled(**paths).alerted == 3


# --- T6.3: notify raises mid-run ---------------------------------------------------

def test_t6_3_failed_notify_leaves_the_ledger_byte_identical(paths, wire):
    """MANDATORY. Notify happens before the ledger write, so a raising notifier must
    leave the file on disk untouched - not rewritten, not truncated."""
    wire(FakeAdapter(jobs(2)))
    runner.run_scheduled(**paths)
    before = paths["ledger_path"].read_bytes()

    wire(FakeAdapter(jobs(4)), notify_raises=RuntimeError("ntfy exploded"))
    with pytest.raises(RuntimeError, match="ntfy exploded"):
        runner.run_scheduled(**paths)

    assert paths["ledger_path"].read_bytes() == before


def test_t6_3_the_two_new_postings_are_alerted_on_the_next_run(paths, wire):
    """The corollary: nothing was lost, it was deferred."""
    wire(FakeAdapter(jobs(2)))
    runner.run_scheduled(**paths)
    wire(FakeAdapter(jobs(4)), notify_raises=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        runner.run_scheduled(**paths)
    sent = wire(FakeAdapter(jobs(4)))
    third = runner.run_scheduled(**paths)
    assert third.alerted == 2
    assert len(sent["digests"][0]) == 2


# --- T6.4: commit fails after a successful notify ----------------------------------

def test_t6_4_a_lost_ledger_write_re_alerts_and_says_so(paths, wire):
    """A failed `git commit` means the next run starts from the OLD ledger. Simulated by
    rolling the file back. The run must re-alert and report the count, not crash."""
    wire(FakeAdapter(jobs(3)))
    runner.run_scheduled(**paths)
    stale = paths["ledger_path"].read_bytes()

    sent = wire(FakeAdapter(jobs(5)))
    runner.run_scheduled(**paths)
    assert sent["digests"][0] and len(sent["digests"][0]) == 2  # the two new ones

    paths["ledger_path"].write_bytes(stale)  # the commit never landed
    wire(FakeAdapter(jobs(5)))
    again = runner.run_scheduled(**paths)
    assert again.alerted == 2
    assert again.retried == 0  # they were absent from the stale ledger, so genuinely new
    assert "retried=" in again.summary()


def test_retried_counts_postings_delivered_after_an_earlier_failure(paths, wire):
    """When the ledger DID record the posting but with alerted_at null, delivering it now
    is a retry and is reported as one."""
    wire(FakeAdapter(jobs(2)), digest_ok=False)  # stored, delivery reported as failed
    first = runner.run_scheduled(**paths)
    assert first.alerted == 0
    assert ledger.load(paths["ledger_path"])      # the postings ARE recorded
    assert alerted_ids(paths) == set()            # but nothing was delivered

    wire(FakeAdapter(jobs(2)))
    second = runner.run_scheduled(**paths)
    assert second.alerted == 2
    assert second.retried == 2


# --- T6.5 / T6.6: the zero-result canary -------------------------------------------

def test_t6_5_canary_fires_when_an_adapter_drops_to_zero(paths, wire):
    """MANDATORY. >0 last run, 0 now, HTTP 200 throughout - the silent-breakage case."""
    wire(FakeAdapter(jobs(3)))
    first = runner.run_scheduled(**paths)
    assert first.canaries == []

    wire(FakeAdapter([]))  # 200 with an empty list
    second = runner.run_scheduled(**paths)
    assert len(second.canaries) == 1
    canary = second.canaries[0]
    assert canary.adapter == "fake"
    assert canary.previous == 3 and canary.current == 0
    assert "3 postings last run, 0 now" in str(canary)


def test_t6_6_zero_last_run_and_zero_now_is_not_a_canary(paths, wire):
    """Not a regression. An adapter that has been quiet all along is old news."""
    wire(FakeAdapter([]))
    runner.run_scheduled(**paths)
    wire(FakeAdapter([]))
    assert runner.run_scheduled(**paths).canaries == []


def test_canary_does_not_fire_on_the_very_first_run(paths, wire):
    """No previous health file means no baseline, not a regression against zero."""
    wire(FakeAdapter([]))
    assert runner.run_scheduled(**paths).canaries == []


def test_pipeline_alert_is_distinct_from_a_job_alert():
    """Build step 6.4: visibly distinct. Different title prefix and max priority."""
    import httpx

    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    import os

    os.environ["JOBMON_NTFY_TOPIC"] = "t"
    from jobmon import notify

    notify.send_pipeline_alert(client, "fake: 3 postings last run, 0 now")
    assert captured[0].headers["X-Title"].startswith("!!")
    assert captured[0].headers["X-Priority"] == "5"


# --- T6.7: one adapter raises, the others complete ---------------------------------

def test_t6_7_a_raising_adapter_does_not_stop_the_others(paths, wire, monkeypatch):
    good = FakeAdapter(jobs(2))
    bad = FakeAdapter([], raises=RuntimeError("board exploded"))
    bad.name = "bad"
    monkeypatch.setattr(runner, "enabled_adapters", lambda: [bad, good])
    monkeypatch.setattr(runner.notify, "send_instant", lambda c, cand, prof: True)
    monkeypatch.setattr(runner.notify, "send_profile_digest",
                        lambda c, cands, prof, truncated=0, held=None, **kw: True)
    result = runner.run_scheduled(**paths)
    assert result.adapters_failed == 1
    assert result.new == 2  # the good adapter still ran
    health = {h.adapter: h for h in result.health}
    assert health["bad"].ok is False and "board exploded" in health["bad"].error
    assert health["fake"].ok is True


# --- T6.9: ledger shape ------------------------------------------------------------

def test_t6_9_ledger_is_valid_jsonl_one_line_per_global_id(paths, wire):
    for _ in range(3):
        wire(FakeAdapter(jobs(4)))
        runner.run_scheduled(**paths)
    lines = paths["ledger_path"].read_text(encoding="utf-8").strip().splitlines()
    ids = [json.loads(line)["global_id"] for line in lines]
    assert len(ids) == 4
    assert len(set(ids)) == len(ids)  # no duplicates


def test_ledger_lines_are_sorted_for_a_stable_diff(paths, wire):
    wire(FakeAdapter(jobs(5)))
    runner.run_scheduled(**paths)
    ids = [json.loads(x)["global_id"]
           for x in paths["ledger_path"].read_text(encoding="utf-8").splitlines()]
    assert ids == sorted(ids)


def test_ledger_does_not_store_descriptions(paths, wire):
    """They are the bulk of a posting and they come back on the next fetch. A committed
    file must not grow by megabytes per run."""
    wire(FakeAdapter(jobs(2)))
    runner.run_scheduled(**paths)
    row = json.loads(paths["ledger_path"].read_text(encoding="utf-8").splitlines()[0])
    assert "description_text" not in row and "description_html" not in row
    assert set(row) == {
        "global_id", "source", "company", "title", "normalized_title", "first_location",
        "apply_url", "first_seen_at", "last_seen_at", "last_seen_run", "reposted_count",
    }


# --- rebuild preserves the dedup rules the cache would otherwise forget ------------

def test_fuzzy_dedup_survives_a_cache_rebuild(paths, wire):
    """The reason the ledger carries the fuzzy key. Same company+title+location under a
    NEW global_id, one run later: without the key in the ledger this would alert again."""
    wire(FakeAdapter([greenhouse_job(1)]))
    assert runner.run_scheduled(**paths).alerted == 1
    wire(FakeAdapter([greenhouse_job(2)]))  # reposted under a new req id
    second = runner.run_scheduled(**paths)
    assert second.duplicates == 1
    assert second.alerted == 0


def test_repost_count_survives_a_cache_rebuild(paths, wire):
    """The reason the ledger carries `last_seen_run`."""
    wire(FakeAdapter([greenhouse_job(1)]))
    runner.run_scheduled(**paths)
    wire(FakeAdapter([]))                      # disappeared
    runner.run_scheduled(**paths)
    wire(FakeAdapter([greenhouse_job(1)]))     # came back
    runner.run_scheduled(**paths)
    entry = ledger.load(paths["ledger_path"])["greenhouse:acme:1"]
    assert entry.reposted_count == 1


def test_run_ids_stay_monotonic_across_rebuilds(paths, wire):
    for _ in range(3):
        wire(FakeAdapter([greenhouse_job(1)]))
        runner.run_scheduled(**paths)
    assert ledger.load(paths["ledger_path"])["greenhouse:acme:1"].last_seen_run == 3


# --- robustness --------------------------------------------------------------------

def test_missing_ledger_is_an_empty_ledger_not_an_error(tmp_path):
    assert ledger.load(tmp_path / "nope.jsonl") == {}


def test_a_corrupt_line_is_skipped_not_fatal(tmp_path, caplog):
    """Losing one posting's history costs a duplicate alert; refusing to start costs
    every alert."""
    path = tmp_path / "seen.jsonl"
    good = json.dumps({"global_id": "a:b:1"})
    path.write_text(f"{good}\n{{not json\n" + json.dumps({"global_id": "a:b:2"}) + "\n",
                    encoding="utf-8")
    entries = ledger.load(path)
    assert set(entries) == {"a:b:1", "a:b:2"}


def test_an_unknown_field_is_ignored_and_the_entry_still_loads(tmp_path):
    """An unknown key is DROPPED, not fatal and not a skipped line.

    This changed with the ledger split (addendum 2): reading a file written by a newer
    version, or a legacy `seen.jsonl` still carrying `alerted_at`, has to cost nothing.
    Losing the line entirely would re-alert that posting - the failure the ledger exists
    to prevent.
    """
    path = tmp_path / "postings.jsonl"
    path.write_text(
        json.dumps({"global_id": "a:b:1", "alerted_at": "2026-01-01", "surprise": 1}) + "\n",
        encoding="utf-8",
    )
    entries = ledger.load(path)
    assert set(entries) == {"a:b:1"}
    assert not hasattr(entries["a:b:1"], "surprise")


def test_a_malformed_line_is_skipped_not_fatal(tmp_path):
    """Losing one posting's history costs a duplicate alert; refusing to start costs
    every alert."""
    path = tmp_path / "postings.jsonl"
    path.write_text('{"global_id": "a:b:1"}\nnot json at all\n', encoding="utf-8")
    assert set(ledger.load(path)) == {"a:b:1"}


def test_dry_run_does_not_move_the_ledger(paths, wire):
    wire(FakeAdapter(jobs(2)))
    runner.run_scheduled(**paths)
    before = paths["ledger_path"].read_bytes()
    wire(FakeAdapter(jobs(5)))
    runner.run_scheduled(dry_run=True, **paths)
    assert paths["ledger_path"].read_bytes() == before


def test_the_derived_cache_is_never_left_behind(paths, wire, tmp_path):
    """SQLite is a temp-dir artifact. Nothing but the ledger and health file persists."""
    wire(FakeAdapter(jobs(2)))
    runner.run_scheduled(**paths)
    assert sorted(p.name for p in (tmp_path / "state").iterdir()) == [
        "alerted", "health.json", "postings.jsonl"
    ]
    assert [p.name for p in (tmp_path / "state" / "alerted").iterdir()] == ["default.jsonl"]
    assert not list(tmp_path.glob("*.db"))
