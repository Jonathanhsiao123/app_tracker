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
    return tmp_path / "state" / "seen.jsonl"


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
    monkeypatch.setattr(runner.notify, "send_alert", lambda c, p, a: True)
    monkeypatch.setattr(runner.notify, "send_digest", lambda c, items, label: True)


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
    assert "adapter failed" in pipeline_alerts[0]
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
