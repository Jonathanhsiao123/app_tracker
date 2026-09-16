"""What `jobmon run` returns to the shell, and why it matters.

A red GitHub Actions run sends an email. This project's entire premise is that the things
which reach you are job postings, so the exit code decides between "push it to the phone
and stay green" and "go red, because there is no phone channel to use". These tests pin
that split down.

No network: the adapter and the notifier are both replaced.
"""

import pytest
from test_runner import FakeAdapter, greenhouse_job  # same dir; tests is not a package

from jobmon import cli, runner


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "state" / "postings.jsonl"


@pytest.fixture
def conn_tier():
    from jobmon import store
    c = store.connect(":memory:")
    store.init_db(c)
    yield c
    c.close()


@pytest.fixture
def topic(monkeypatch):
    monkeypatch.setenv("JOBMON_NTFY_TOPIC", "test-topic")


@pytest.fixture
def pipeline_alerts(monkeypatch):
    """Capture pipeline alerts, and stub the client they are sent with."""
    sent = []
    monkeypatch.setattr(cli.notify, "send_pipeline_alert",
                        lambda client, message: sent.append(message) or True)
    monkeypatch.setattr(cli.ats_public, "make_client", _NullClient)
    return sent


class _NullClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run_cli(ledger_path, *extra):
    return cli.main(["run", "--ledger", str(ledger_path), *extra])


def install(monkeypatch, adapter):
    monkeypatch.setattr(runner, "enabled_adapters", lambda: [adapter])
    monkeypatch.setattr(runner.notify, "send_instant", lambda c, cand, prof: True)
    monkeypatch.setattr(runner.notify, "send_profile_digest",
                        lambda c, cands, prof, truncated=0, held=None: True)


# --- the missing secret: red, but say which secret ----------------------------------

def test_an_unset_topic_exits_misconfigured_before_fetching_anything(
    monkeypatch, ledger_path, capsys
):
    """The old behaviour raised inside the notifier AFTER sweeping 169 boards, so the
    only signal was a traceback and an email saying "workflow failed"."""
    monkeypatch.delenv("JOBMON_NTFY_TOPIC", raising=False)
    adapter = FakeAdapter([greenhouse_job(1)])
    install(monkeypatch, adapter)

    assert run_cli(ledger_path) == cli.EXIT_MISCONFIGURED
    assert adapter.calls == 0, "refused before spending the sweep"
    assert "JOBMON_NTFY_TOPIC" in capsys.readouterr().err


def test_dry_run_needs_no_topic(monkeypatch, ledger_path):
    monkeypatch.delenv("JOBMON_NTFY_TOPIC", raising=False)
    install(monkeypatch, FakeAdapter([greenhouse_job(1)]))
    assert run_cli(ledger_path, "--dry-run") == cli.EXIT_OK


def test_seed_needs_no_topic(monkeypatch, ledger_path):
    """Seeding sends nothing by definition, so it must not require a delivery channel."""
    monkeypatch.delenv("JOBMON_NTFY_TOPIC", raising=False)
    install(monkeypatch, FakeAdapter([greenhouse_job(1)]))
    assert run_cli(ledger_path, "--seed") == cli.EXIT_OK
    assert ledger_path.exists()


# --- a working run stays green ------------------------------------------------------

def test_a_clean_run_exits_zero(monkeypatch, ledger_path, topic, pipeline_alerts):
    install(monkeypatch, FakeAdapter([greenhouse_job(1), greenhouse_job(2)]))
    assert run_cli(ledger_path) == cli.EXIT_OK
    assert pipeline_alerts == []


# --- pipeline problems: pushed, and NOT red ------------------------------------------

def test_a_failed_adapter_is_pushed_and_does_not_fail_the_run(
    monkeypatch, ledger_path, topic, pipeline_alerts
):
    """Previously this returned 1 and the sole notice was a red run, i.e. an email."""
    install(monkeypatch, FakeAdapter(None, raises=RuntimeError("board is 404")))

    assert run_cli(ledger_path) == cli.EXIT_PIPELINE_REPORTED
    assert len(pipeline_alerts) == 1
    assert "adapter problem" in pipeline_alerts[0]
    assert "board is 404" in pipeline_alerts[0]


def test_a_zero_result_canary_is_pushed_and_does_not_fail_the_run(
    monkeypatch, ledger_path, topic, pipeline_alerts
):
    health_path = ledger_path.parent / "health.json"
    install(monkeypatch, FakeAdapter([greenhouse_job(1)]))
    assert run_cli(ledger_path) == cli.EXIT_OK

    # Same adapter, now returning nothing: some -> none is the alarm (T6.5).
    install(monkeypatch, FakeAdapter([]))
    assert run_cli(ledger_path) == cli.EXIT_PIPELINE_REPORTED
    assert health_path.exists()
    assert len(pipeline_alerts) == 1
    assert "zero-result regression" in pipeline_alerts[0]


def test_a_dry_run_reports_problems_without_sending(
    monkeypatch, ledger_path, topic, pipeline_alerts
):
    install(monkeypatch, FakeAdapter(None, raises=RuntimeError("nope")))
    assert run_cli(ledger_path, "--dry-run") == cli.EXIT_PIPELINE_REPORTED
    assert pipeline_alerts == []


def test_mutually_exclusive_flags_exit_misconfigured(ledger_path, topic):
    assert run_cli(ledger_path, "--dry-run", "--seed") == cli.EXIT_MISCONFIGURED


# --- one bad posting must not cost the other 12,999 ---------------------------------

class ExplodingNormalize:
    """`normalize.normalize` that raises on one specific posting."""

    def __init__(self, bad_id):
        self.bad_id = bad_id
        self.real = None

    def __call__(self, raw):
        if raw.get("id") == self.bad_id:
            raise ValueError("posted_at is not a date")
        return self.real(raw)


@pytest.fixture
def boom(monkeypatch):
    def install(bad_id):
        exploding = ExplodingNormalize(bad_id)
        exploding.real = runner.normalize.normalize
        monkeypatch.setattr(runner.normalize, "normalize", exploding)
    return install


def test_one_unparseable_posting_does_not_abort_the_sweep(
    monkeypatch, ledger_path, topic, pipeline_alerts, boom
):
    """The regression that turned every cron run red: normalize() sat inside the
    adapter-wide try, so posting #2 raising dropped #3..#N and failed the adapter."""
    # Distinct titles, or the fuzzy 60-day dedup folds them into one another.
    jobs = [greenhouse_job(i, title=f"SWE New Grad {i}") for i in range(1, 41)]
    install(monkeypatch, FakeAdapter(jobs))
    boom(2)  # 1 of 40 = 2.5%, under MALFORMED_ALERT_RATIO

    assert run_cli(ledger_path) == cli.EXIT_OK
    assert pipeline_alerts == [], "one bad posting is data noise, not a pipeline failure"

    # The other 39 are in the ledger; the sweep did not stop at #2.
    stored = [line for line in ledger_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(stored) == 39


def test_a_wholesale_parse_failure_is_reported(
    monkeypatch, ledger_path, topic, pipeline_alerts, capsys
):
    """Above MALFORMED_ALERT_RATIO it is an ATS schema change, not noise - and the
    zero-result canary cannot see it, because the raw postings still arrive."""
    monkeypatch.setattr(runner, "MALFORMED_ALERT_RATIO", 0.05)
    jobs = [greenhouse_job(i) for i in range(1, 6)]
    install(monkeypatch, FakeAdapter(jobs))
    monkeypatch.setattr(
        runner.normalize, "normalize",
        lambda raw: (_ for _ in ()).throw(ValueError("schema changed")),
    )

    assert run_cli(ledger_path) == cli.EXIT_PIPELINE_REPORTED
    assert len(pipeline_alerts) == 1
    assert "could not be stored" in pipeline_alerts[0]
    assert "malformed=5" in capsys.readouterr().out


# --- a cold sweep must not try to push 697 times ------------------------------------

def test_all_openings_are_sent_in_one_digest(monkeypatch, conn_tier):
    """A cold sweep of NON-tier companies is delivered as one batch, so no opening needs
    an individual push.

    Since the addendum a tier-1 company does get an individual instant alert (that is the
    point of the tier list), so this uses an ordinary company and asserts the batch.
    """
    from datetime import UTC, datetime

    from jobmon import notify, profiles, runner, store
    from jobmon.models import Analysis, Posting

    sendable = []
    for i in range(50):
        posting = Posting(
            global_id=f"gh:acme:{i}", source="greenhouse", company="Acme",
            title=f"SWE {i}", locations=["NYC"], description_html="", description_text="",
            apply_url=f"https://x/{i}",
            first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC),
        )
        store.upsert_posting(conn_tier, posting, 1)
        sendable.append((posting, Analysis(
            global_id=posting.global_id, role_class="new_grad", function=["swe"],
            eligibility="unknown", eligibility_evidence=[], keywords={}, gaps=[],
            recommended_variant="swe", variant_scores={},
        )))

    calls = {"alerts": 0, "digests": []}
    monkeypatch.setattr(notify, "send_instant",
                        lambda c, cand, prof: calls.__setitem__("alerts", calls["alerts"] + 1)
                        or True)
    monkeypatch.setattr(notify, "send_profile_digest",
                        lambda c, cands, prof, truncated=0, held=None:
                        calls["digests"].append(len(cands)) or True)

    outcome = runner._deliver_profile(
        None, conn_tier, profiles.Profile(), [p for p, _ in sendable],
        _config(), tier=set(),
    )

    assert calls["alerts"] == 0
    assert calls["digests"] == [50]
    assert outcome.alerted == 50, "every posting is still marked alerted"


def _config():
    from jobmon import config_loader

    return config_loader.load()


def test_digest_orders_newest_openings_first():
    from datetime import UTC, datetime

    from jobmon import notify
    from jobmon.models import Analysis, Posting

    def item(gid, posted_at):
        posting = Posting(
            global_id=gid, source="greenhouse", company="Acme", title=gid,
            locations=["NYC"], description_html="", description_text="", apply_url="https://x",
            first_seen_at=posted_at, last_seen_at=posted_at, posted_at=posted_at,
        )
        analysis = Analysis(
            global_id=gid, role_class="new_grad", function=["swe"],
            eligibility="likely_ok", eligibility_evidence=[], keywords={}, gaps=[],
            recommended_variant="swe", variant_scores={},
        )
        return (posting, analysis)

    old = item("gh:acme:0001", datetime(2026, 9, 14, tzinfo=UTC))
    recent = item("gh:acme:9999", datetime(2026, 9, 15, tzinfo=UTC))
    ranked = sorted([old, recent], key=notify.digest_rank)
    assert ranked[0] is recent
