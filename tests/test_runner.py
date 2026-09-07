"""Runner: one full pipeline pass (spec 3, build step 3).

No network and no notifications - the adapter and the notifier are both replaced. What is
under test is the wiring: what gets alerted, what gets suppressed, what survives a failure.
"""

from datetime import UTC, datetime

import pytest

from jobmon import runner, store


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    yield c
    c.close()


def greenhouse_job(job_id, title="Software Engineer, New Grad", body="<p>Build things.</p>"):
    return {
        "_ats": "greenhouse",
        "_company": "Acme",
        "id": job_id,
        "title": title,
        "content": body,
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "location": {"name": "NYC"},
        "updated_at": "2026-09-01T00:00:00Z",
    }


class FakeAdapter:
    """Stands in for `ats_public`. `jobs=None` raises, to test independent failure."""

    name = "fake"

    def __init__(self, jobs, raises=None):
        self.jobs = jobs
        self.raises = raises
        self.calls = 0

    def fetch(self, client, companies=None, **kw):
        self.calls += 1
        if self.raises:
            raise self.raises
        yield from self.jobs


@pytest.fixture
def wire(monkeypatch):
    """Install a fake adapter and capture everything the notifier would send.

    Each call resets the capture, so a second `wire(...)` in one test asserts only on what
    that run sent rather than on an accumulator holding the previous run's output too.
    """
    sent = {"alerts": [], "digests": []}

    def install(adapter, alert_ok=True, digest_ok=True):
        sent["alerts"].clear()
        sent["digests"].clear()
        monkeypatch.setattr(runner, "enabled_adapters", lambda: [adapter])
        monkeypatch.setattr(
            runner.notify, "send_alert",
            lambda c, p, a: (sent["alerts"].append(p.global_id), alert_ok)[1],
        )
        monkeypatch.setattr(
            runner.notify, "send_digest",
            lambda c, items, label: (sent["digests"].append([p.global_id for p, _ in items]),
                                     digest_ok)[1],
        )
        return sent

    return install


# --- the gate, through the whole runner --------------------------------------------

def test_run1_alerts_n_run2_alerts_zero(conn, wire):
    adapter = FakeAdapter([greenhouse_job(i, title=f"SWE New Grad {i}") for i in range(4)])
    sent = wire(adapter)
    first = runner.run(conn)
    assert first.new == 4
    assert first.alerted == 4
    second = runner.run(conn)
    assert second.new == 0
    assert second.alerted == 0
    assert len(sent["digests"]) == 1  # only the first run sent anything


def test_health_row_is_recorded_every_run(conn, wire):
    wire(FakeAdapter([greenhouse_job(1)]))
    result = runner.run(conn)
    assert len(result.health) == 1
    assert result.health[0].ok is True
    assert result.health[0].postings_seen == 1
    assert store.health_report(conn)[0].adapter == "fake"


# --- failed delivery must be retried, not lost -------------------------------------

def test_failed_delivery_is_retried_next_run(conn, wire):
    """The bug this test exists for: alerting keyed off "the upsert said new" would drop a
    posting forever the moment one notification failed. It is keyed off `alerted_at`."""
    adapter = FakeAdapter([greenhouse_job(1)])
    sent = wire(adapter, digest_ok=False)
    first = runner.run(conn)
    assert first.new == 1
    assert first.alerted == 0                       # delivery failed
    assert len(sent["digests"]) == 1                # but it was attempted

    # Same posting, second run: no longer "new", still never delivered.
    sent2 = wire(FakeAdapter([greenhouse_job(1)]), digest_ok=True)
    second = runner.run(conn)
    assert second.new == 0
    assert second.alerted == 1                      # retried and delivered
    assert sent2["digests"][-1] == ["greenhouse:acme:1"]


def test_delivered_posting_is_not_resent(conn, wire):
    wire(FakeAdapter([greenhouse_job(1)]))
    runner.run(conn)
    sent = wire(FakeAdapter([greenhouse_job(1)]))
    assert runner.run(conn).alerted == 0
    assert sent["digests"] == []


# --- seed --------------------------------------------------------------------------

def test_seed_marks_alerted_without_sending(conn, wire):
    """The first pass against real boards finds every currently-open posting - over 13,000.
    `--seed` is the only thing standing between a fresh database and that many pushes."""
    adapter = FakeAdapter([greenhouse_job(i, title=f"SWE New Grad {i}") for i in range(3)])
    sent = wire(adapter)
    result = runner.run(conn, seed=True)
    assert result.seeded == 3
    assert result.alerted == 0
    assert sent["alerts"] == [] and sent["digests"] == []
    # And the next real run stays quiet.
    wire(FakeAdapter([greenhouse_job(i, title=f"SWE New Grad {i}") for i in range(3)]))
    assert runner.run(conn).alerted == 0


def test_dry_run_sends_nothing_and_does_not_mark_alerted(conn, wire, capsys):
    sent = wire(FakeAdapter([greenhouse_job(1)]))
    result = runner.run(conn, dry_run=True)
    assert result.alerted == 0
    assert sent["alerts"] == [] and sent["digests"] == []
    assert "boards.greenhouse.io" in capsys.readouterr().out  # printed the payload
    # Not marked: a real run afterwards still delivers it.
    sent2 = wire(FakeAdapter([greenhouse_job(1)]))
    assert runner.run(conn).alerted == 1
    assert sent2["digests"]


# --- suppression -------------------------------------------------------------------

def test_experienced_postings_are_suppressed(conn, wire):
    sent = wire(FakeAdapter([
        greenhouse_job(1, title="Senior Software Engineer"),
        greenhouse_job(2, title="Software Engineer, New Grad"),
    ]))
    result = runner.run(conn)
    assert result.new == 2          # both stored, nothing is deleted
    assert result.alerted == 1      # only one alerted
    assert sent["digests"][0] == ["greenhouse:acme:2"]


def test_suppressed_posting_is_still_stored(conn, wire):
    wire(FakeAdapter([greenhouse_job(1, title="Staff Software Engineer")]))
    runner.run(conn)
    assert store.get_posting(conn, "greenhouse:acme:1") is not None


# --- adapter failure is independent ------------------------------------------------

def test_adapter_exception_does_not_kill_the_run(conn, wire):
    wire(FakeAdapter([], raises=RuntimeError("board exploded")))
    result = runner.run(conn)
    assert result.adapters_failed == 1
    assert result.health[0].ok is False
    assert "board exploded" in result.health[0].error


def test_zero_results_without_error_is_recorded_as_ok(conn, wire):
    """The silent-breakage signal: ok=True with postings_seen=0. The canary in build step 6
    compares this against the previous run; the runner only has to record it faithfully."""
    wire(FakeAdapter([]))
    result = runner.run(conn)
    assert result.health[0].ok is True
    assert result.health[0].postings_seen == 0


def test_unparseable_payload_is_counted_not_fatal(conn, wire):
    wire(FakeAdapter([{"_ats": "greenhouse", "_company": "Acme", "id": 1},  # no title
                      greenhouse_job(2)]))
    result = runner.run(conn)
    assert result.normalize_dropped == 1
    assert result.new == 1


# --- analysis ----------------------------------------------------------------------

def test_analysis_row_is_written_for_every_alertable_posting(conn, wire):
    wire(FakeAdapter([greenhouse_job(1)]))
    runner.run(conn)
    analysis = store.get_analysis(conn, "greenhouse:acme:1")
    assert analysis is not None
    assert analysis.role_class == "new_grad"


def test_citizenship_only_posting_is_blocked_and_suppressed(conn, wire):
    """Build step 4 is wired in: this used to alert."""
    sent = wire(FakeAdapter([greenhouse_job(1, body="<p>Must be a U.S. citizen.</p>")]))
    result = runner.run(conn)
    analysis = store.get_analysis(conn, "greenhouse:acme:1")
    assert analysis.eligibility == "blocked"
    assert analysis.eligibility_evidence == ["Must be a U.S. citizen."]  # verbatim
    assert result.blocked == 1
    assert result.alerted == 0
    assert sent["digests"] == []


def test_blocked_posting_is_stored_and_retrievable_via_show_blocked(conn, wire):
    """T4c: nothing is deleted. `--show-blocked` is a presentation-layer flag only."""
    wire(FakeAdapter([greenhouse_job(1, body="<p>Requires an active TS/SCI clearance.</p>")]))
    runner.run(conn)
    assert store.get_posting(conn, "greenhouse:acme:1") is not None
    assert list(store.iter_postings(conn, include_blocked=False)) == []
    assert len(list(store.iter_postings(conn, include_blocked=True))) == 1


def test_unknown_eligibility_still_alerts(conn, wire):
    """`unknown` is the majority case and must never be treated as blocked (spec 6)."""
    sent = wire(FakeAdapter([greenhouse_job(1, body="<p>We build distributed systems.</p>")]))
    result = runner.run(conn)
    assert store.get_analysis(conn, "greenhouse:acme:1").eligibility == "unknown"
    assert result.alerted == 1
    assert sent["digests"] == [["greenhouse:acme:1"]]


def test_keywords_and_gaps_are_populated_from_the_taxonomy(conn, wire):
    """Build step 5 is wired in. Kubernetes is a real gap (no infra in the inventory);
    Python and PyTorch are held, so they are matched but are not gaps."""
    wire(FakeAdapter([greenhouse_job(
        1, body="<p>Experience with Python, Kubernetes and PyTorch required.</p>")]))
    runner.run(conn)
    analysis = store.get_analysis(conn, "greenhouse:acme:1")
    assert "python" in analysis.keywords.get("languages", [])
    assert "pytorch" in analysis.keywords.get("ml_frameworks", [])
    assert "kubernetes" in analysis.keywords.get("infra", [])
    assert analysis.gaps == ["kubernetes"]


def test_variant_scores_are_no_longer_all_zero(conn, wire):
    """Before step 5, `keywords` was empty so every variant scored 0.0 and `recommend`
    always fell back to generalist. Real matches make the scores mean something."""
    wire(FakeAdapter([greenhouse_job(
        1, title="Machine Learning Engineer",
        body="<p>Adversarial robustness research with PyTorch. Experience with Python.</p>")]))
    runner.run(conn)
    analysis = store.get_analysis(conn, "greenhouse:acme:1")
    assert any(v > 0 for v in analysis.variant_scores.values())
    assert analysis.recommended_variant == "mle"


def test_cosmetic_gap_names_its_substitute_in_the_stored_record(conn, wire):
    wire(FakeAdapter([greenhouse_job(
        1, body="<p>Experience with JAX and Python programming required.</p>")]))
    runner.run(conn)
    analysis = store.get_analysis(conn, "greenhouse:acme:1")
    assert analysis.gaps == ["jax (~pytorch)"]


def test_reanalyze_does_not_cause_a_realert(conn, wire):
    wire(FakeAdapter([greenhouse_job(1)]))
    runner.run(conn)
    before = conn.execute(
        "SELECT alerted_at FROM postings WHERE global_id = 'greenhouse:acme:1'"
    ).fetchone()["alerted_at"]
    assert runner.analyze_pending(conn, reanalyze_all=True) == 1
    after = conn.execute(
        "SELECT alerted_at FROM postings WHERE global_id = 'greenhouse:acme:1'"
    ).fetchone()["alerted_at"]
    assert before == after


def test_analyze_pending_only_touches_rows_without_analysis(conn, wire):
    wire(FakeAdapter([greenhouse_job(1)]))
    runner.run(conn)
    assert runner.analyze_pending(conn) == 0  # the run already analyzed it


# --- tier 1 routing ----------------------------------------------------------------

def test_tier1_company_gets_an_individual_alert(conn, wire, monkeypatch):
    monkeypatch.setattr(runner.config_loader, "tier_companies", lambda cfg: {"Acme"})
    sent = wire(FakeAdapter([greenhouse_job(1)]))
    result = runner.run(conn)
    assert sent["alerts"] == ["greenhouse:acme:1"]
    assert sent["digests"] == []
    assert result.alerted == 1


def test_mark_alerted_timestamp_is_set_after_delivery(conn, wire):
    wire(FakeAdapter([greenhouse_job(1)]))
    runner.run(conn)
    row = conn.execute(
        "SELECT alerted_at FROM postings WHERE global_id = 'greenhouse:acme:1'"
    ).fetchone()
    assert row["alerted_at"] is not None
    assert datetime.fromisoformat(row["alerted_at"]).tzinfo is not None
    assert datetime.fromisoformat(row["alerted_at"]) <= datetime.now(UTC)
