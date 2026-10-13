"""Runner: one full pipeline pass (spec 3, build step 3).

No network and no notifications - the adapter and the notifier are both replaced. What is
under test is the wiring: what gets alerted, what gets suppressed, what survives a failure.
"""

import hashlib
from datetime import UTC, datetime

import pytest

from jobmon import runner, store
from jobmon.models import Posting


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
        "updated_at": datetime.now(UTC).isoformat(),
    }


def github_repo_job(apply_url, title="Software Engineer, New Grad", company="Acme",
                     locations=("NYC",), closed=False):
    return {
        "_ats": "github_repo",
        "_company": company,
        "company": company,
        "title": title,
        "locations": list(locations),
        "apply_url": apply_url,
        "posted_text": "0d",
        "closed": closed,
        "repo_kind": "new_grad",
        "source_repo": "https://example/repo",
    }


def github_repo_global_id(company, apply_url):
    """`from_github_repo` hashes `apply_url` for the id (no natural one exists); mirrored
    here so a test can look the stored posting up without reaching into normalize.py."""
    job_id = hashlib.sha256(apply_url.encode()).hexdigest()[:16]
    return Posting.make_global_id("github_repo", company, job_id)


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
        # Since the addendum the runner sends per profile: `send_instant` for a tier-1 or
        # high-score posting, `send_profile_digest` for the batch. The capture shape is
        # unchanged so the assertions below still read the same way.
        monkeypatch.setattr(
            runner.notify, "send_instant",
            lambda c, cand, prof: (sent["alerts"].append(cand.global_id), alert_ok)[1],
        )
        monkeypatch.setattr(
            runner.notify, "send_profile_digest",
            lambda c, cands, prof, truncated=0, held=None, **kw: (
                sent["digests"].append([x.global_id for x in cands]), digest_ok)[1],
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
    assert len(sent["digests"]) == 2  # the second run sends the empty hourly heartbeat


def test_health_row_is_recorded_every_run(conn, wire):
    wire(FakeAdapter([greenhouse_job(1)]))
    result = runner.run(conn)
    assert len(result.health) == 1
    assert result.health[0].ok is True
    assert result.health[0].postings_seen == 1
    assert store.health_report(conn)[0].adapter == "fake"


def test_cross_source_dedup_github_repo_and_ats_board(conn, wire):
    """T7.5: the same job reachable both through a curated repo and through the ATS sweep
    must alert once, not twice. The secondary fuzzy dedup key is (company, normalized
    title, first location) - it does not care which adapter produced either row, so one
    `FakeAdapter` yielding both raw shapes in the same run is a faithful stand-in for two
    real adapters finding the same posting."""
    sent = wire(FakeAdapter([
        greenhouse_job(1),  # Acme, "Software Engineer, New Grad", NYC
        github_repo_job("https://jobs.lever.co/acme/dup", company="Acme",
                         title="Software Engineer, New Grad", locations=("NYC",)),
    ]))
    result = runner.run(conn)
    assert result.new == 1
    assert result.duplicates == 1
    assert result.alerted == 1
    assert len(sent["digests"][0]) == 1


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
    assert sent["digests"] == [[]]


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


def test_closed_github_repo_row_is_suppressed_but_stored(conn, wire):
    """The `closed` flag a github_repo row carries (build step 7b) suppresses the same way
    `blocked` does - stored, counted, not sent."""
    url = "https://jobs.lever.co/acme/closed-1"
    sent = wire(FakeAdapter([github_repo_job(url, closed=True)]))
    result = runner.run(conn)
    assert result.new == 1
    assert result.closed == 1
    assert result.alerted == 0
    assert sent["digests"] == [[]]
    assert store.get_posting(conn, github_repo_global_id("Acme", url)) is not None


def test_open_github_repo_row_alerts_normally(conn, wire):
    url = "https://jobs.lever.co/acme/open-1"
    sent = wire(FakeAdapter([github_repo_job(url, closed=False)]))
    result = runner.run(conn)
    assert result.closed == 0
    assert result.alerted == 1
    assert sent["digests"][0] == [github_repo_global_id("Acme", url)]


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
    assert sent["digests"] == [[]]


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
    before = store.alerted_at(conn, "greenhouse:acme:1")
    assert runner.analyze_pending(conn, reanalyze_all=True) == 1
    after = store.alerted_at(conn, "greenhouse:acme:1")
    assert before is not None and before == after


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
    # The digest still goes out, empty: it doubles as the hourly heartbeat, so an SMTP
    # recipient can tell a quiet hour from a dead cron.
    assert sent["digests"] == [[]]
    assert result.alerted == 1


def test_mark_alerted_timestamp_is_set_after_delivery(conn, wire):
    wire(FakeAdapter([greenhouse_job(1)]))
    runner.run(conn)
    when = store.alerted_at(conn, "greenhouse:acme:1")
    assert when is not None
    assert when.tzinfo is not None
    assert when <= datetime.now(UTC)


# --- copies stay folded across ledger rebuilds; the clearance screen ---------------------

def test_copies_stay_folded_across_ledger_rebuilds(tmp_path, wire):
    """The bug behind "the same job and link, twice": the `duplicates` table lives in the
    derived cache and is not in the ledger, so a copy folded on the run that inserted it
    came back on the next run as an ordinary undelivered posting. Here the copy also
    defeats both old keys - different company spelling, different title, a tracking
    query string - and only the link ties it to the original."""
    gh = greenhouse_job(8204086123)
    copy = github_repo_job(gh["absolute_url"] + "?utm_source=Simplify&ref=Simplify",
                           company="Acme Industries", title="SWE - New Grad (2027)",
                           locations=("New York, NY",))
    sent = wire(FakeAdapter([gh, copy]))
    state = tmp_path / "state"
    kwargs = {"ledger_path": state, "health_path": state / "health.json"}

    first = runner.run_scheduled(**kwargs)
    assert sent["digests"] == [["greenhouse:acme:8204086123"]]
    assert first.alerted == 1

    wire(FakeAdapter([gh, copy]))
    second = runner.run_scheduled(**kwargs)
    assert second.alerted == 0, "the copy must not arrive an hour later as a new posting"


def _long(text):
    return "<p>" + "We build reliable distributed systems for customers. " * 12 + text + "</p>"


def _screening_profile(**kw):
    from jobmon.profiles import Profile, Screening
    return Profile(name="p", screening=Screening(enabled=True, fetch_pages=False, **kw))


def test_screen_drops_a_clearance_req_the_regex_misses(conn, wire, monkeypatch):
    """The requirement in the last sentence, phrased so `CLEARANCE_PATTERNS` misses it."""
    from jobmon.analyze import screen

    def fake_ask(self, posting, text, basis):
        if "clearance" in text:
            return "clearance", "Must be able to obtain a Secret security clearance.", "swe"
        return "none", "", "swe"

    monkeypatch.setattr(screen.Screener, "_ask", fake_ask)
    sent = wire(FakeAdapter([
        greenhouse_job(1, title="SWE New Grad A", body=_long("Apply today.")),
        greenhouse_job(2, title="SWE New Grad B",
                       body=_long("Must be able to obtain a Secret security clearance.")),
    ]))
    cache = {}
    result = runner.run(conn, profile_list=[_screening_profile()], screen_cache=cache)
    assert sent["digests"] == [["greenhouse:acme:1"]]
    assert result.profiles["p"].screened_out == 1
    assert cache["greenhouse:acme:2"].verdict == "clearance"


def test_screen_fails_open_when_the_api_is_unavailable(conn, wire, monkeypatch):
    """No key or an outage must never empty the digest: deliver under the regex rules."""
    from jobmon.analyze import screen

    def broken(self, posting, text, basis):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(screen.Screener, "_ask", broken)
    sent = wire(FakeAdapter([greenhouse_job(1, title="SWE New Grad A", body=_long("x"))]))
    result = runner.run(conn, profile_list=[_screening_profile()], screen_cache={})
    assert sent["digests"] == [["greenhouse:acme:1"]]
    assert result.profiles["p"].screened_out == 0


def test_screen_toggle_off_makes_no_calls(conn, wire, monkeypatch):
    from jobmon.analyze import screen
    from jobmon.profiles import Profile

    monkeypatch.setattr(screen.Screener, "_ask", lambda *a: pytest.fail("called"))
    wire(FakeAdapter([greenhouse_job(1, body=_long("clearance"))]))
    runner.run(conn, profile_list=[Profile(name="p")], screen_cache={})
    monkeypatch.setenv("JOBMON_LLM_SCREEN", "off")
    wire(FakeAdapter([greenhouse_job(2, body=_long("clearance"))]))
    runner.run(conn, profile_list=[_screening_profile()], screen_cache={})
