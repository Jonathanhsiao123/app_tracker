"""The 2026-09-17 repair: why the hourly mail was empty, and what now keeps it full.

Four defects, each measured against a real sweep before it was touched (see the numbers in
`config/profiles/jonathan.yaml` and `.github/workflows/monitor.yml`):

  1. `unknown` sponsor confidence was charged a penalty it had already paid for in its
     score term, on 321 of 321 candidates - a uniform offset, so `min_score: 0.55` admitted
     nothing at all.
  2. The digest only ever listed postings NEW since the last sweep. Hourly, that is one or
     two items and often zero, so the mail had nothing in it worth opening. `recap_days`
     is the fix: a second section listing what is still open, not just what changed.
  3. `locations.allow` was a scoring hint with an allow-list's name: 57% of alertable
     postings matched neither `allow` nor `prefer`.
  4. The same req arrived from an ATS board and two curated repos under three spellings,
     and the (company, title, location) key caught none of them: 14% redundant copies.

No network in this file. The recap and dedup tests drive a real SQLite cache because both
are queries, and a mock of a query proves nothing about the query.
"""

import argparse
from datetime import UTC, datetime, timedelta

import pytest

from jobmon import cli, ledger, notify, runner, scoring, store
from jobmon.config_loader import rank_weights
from jobmon.models import Analysis, Posting
from jobmon.normalize import ats_job_token
from jobmon.profiles import (
    Eligibility,
    Limits,
    Locations,
    Priorities,
    Profile,
    ProfileError,
    Roles,
    load_profile,
)


def make_posting(gid="ashby:acme:1", company="Acme", title="Software Engineer, New Grad",
                 locations=("New York",), url=None, first_seen_at=None):
    when = first_seen_at or datetime.now(UTC)
    return Posting(
        global_id=gid, source=gid.split(":")[0], company=company, title=title,
        locations=list(locations), description_html="", description_text="",
        apply_url=url or f"https://example.test/{gid}",
        first_seen_at=when, last_seen_at=when, posted_at=when,
    )


def make_analysis(gid="ashby:acme:1", function=("swe",), sponsor_confidence="unknown"):
    return Analysis(
        global_id=gid, role_class="new_grad", function=list(function),
        eligibility="unknown", eligibility_evidence=[], keywords={}, gaps=[],
        recommended_variant="generalist", variant_scores={"generalist": 0.7},
        text_eligibility="unknown", sponsor_confidence=sponsor_confidence,
    )


def make_profile(**overrides):
    base = {
        "name": "t",
        "eligibility": Eligibility(
            needs_sponsorship=True, min_sponsor_confidence="occasional"),
        "roles": Roles(priorities=Priorities(
            order=["mle", "swe"], never=[], sort_by="priority", weights={},
            resolved=rank_weights(["mle", "swe"], {}),
        )),
        "locations": Locations(allow=["United States"], prefer=["New York"]),
        "limits": Limits(min_score=0.0),
    }
    base.update(overrides)
    return Profile(**base)


# --- 1. the penalty that fired on everything -------------------------------------------

def test_unknown_sponsor_confidence_is_not_charged_the_below_minimum_penalty():
    """`unknown` means no DOL match, which is not evidence of refusal.

    It is already scored at 0.5 by CONFIDENCE_SCORE, mid-scale. Charging
    `penalty_below_min_sponsor` on top scored the same missing fact twice.
    """
    profile = make_profile()
    _, breakdown = scoring.score(
        make_posting(), make_analysis(sponsor_confidence="unknown"), profile
    )
    assert "below_min_sponsor" not in breakdown


def test_observed_low_confidence_is_still_charged():
    """The penalty must keep working where there IS evidence - only `unknown` is exempt."""
    profile = make_profile()
    _, breakdown = scoring.score(
        make_posting(), make_analysis(sponsor_confidence="none_observed"), profile
    )
    assert breakdown["below_min_sponsor"] < 0


def test_the_exemption_is_worth_a_tenth_of_the_scale():
    """Regression guard on the actual magnitude, which is what broke `min_score`."""
    profile = make_profile()
    unknown, _ = scoring.score(make_posting(), make_analysis(), profile)
    observed, _ = scoring.score(
        make_posting(), make_analysis(sponsor_confidence="none_observed"), profile
    )
    assert unknown - observed == pytest.approx(
        profile.scoring.penalty_below_min_sponsor
        + profile.scoring.w_eligibility * (0.5 - 0.3)
    )


# --- 3. `allow` as an allow-list --------------------------------------------------------

def test_location_outside_allow_is_suppressed_by_default():
    profile = make_profile()
    ceiling, reasons = scoring.policy_ceiling(
        make_posting(locations=("Sydney, Australia",)), make_analysis(), profile
    )
    assert ceiling == "silent"
    assert any("matches neither allow nor prefer" in r for r in reasons)


def test_location_inside_allow_is_untouched():
    profile = make_profile()
    ceiling, _ = scoring.policy_ceiling(
        make_posting(locations=("Austin, United States",)), make_analysis(), profile
    )
    assert ceiling == "instant"


def test_empty_allow_list_never_suppresses():
    """A profile that has not thought about location must not lose every posting."""
    profile = make_profile(locations=Locations(allow=[], prefer=[]))
    ceiling, _ = scoring.policy_ceiling(
        make_posting(locations=("Sydney, Australia",)), make_analysis(), profile
    )
    assert ceiling == "instant"


def test_on_mismatch_digest_keeps_the_old_lenient_behaviour():
    profile = make_profile(
        locations=Locations(allow=["United States"], on_mismatch="digest")
    )
    ceiling, _ = scoring.policy_ceiling(
        make_posting(locations=("Sydney, Australia",)), make_analysis(), profile
    )
    assert ceiling == "digest"


def test_a_bad_on_mismatch_value_fails_loudly_at_load(tmp_path):
    """T8.12: a config typo must never degrade into 'alert everything'."""
    path = tmp_path / "p.yaml"
    path.write_text("name: p\nlocations:\n  on_mismatch: someday\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="on_mismatch"):
        load_profile(path)


# --- 4. one req, one line ---------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    (("https://jobs.ashbyhq.com/gecko/24561868-f075-4edf-a991-59ff0174e92a"
      "/application?embed=true"), "24561868-f075-4edf-a991-59ff0174e92a"),
    ("https://boards.greenhouse.io/anduril/jobs/5236639007?gh_jid=5236639007", "5236639007"),
    ("https://job-boards.greenhouse.io/nisc/jobs/8204086", "8204086"),
    ("https://wex.wd5.myworkdayjobs.com/en-US/job/US---Remote/Backend_R22589", "r22589"),
    ("https://example.test/careers", None),
    ("", None),
])
def test_ats_job_token_extraction(url, expected):
    assert ats_job_token(url) == expected


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "c.db")
    store.init_db(c)
    yield c
    c.close()


def test_same_req_id_under_different_titles_is_one_posting(conn):
    """The case the fuzzy key could not see: same Ashby id, three spellings."""
    url = "https://jobs.ashbyhq.com/gecko/24561868-f075-4edf-a991-59ff0174e92a"
    run = store.begin_run(conn)
    assert store.upsert_posting(conn, make_posting(
        gid="ashby:gecko:24561868", company="Gecko Robotics",
        title="Embedded Software Engineering Intern", locations=("Pittsburgh",), url=url,
    ), run) == "new"
    assert store.upsert_posting(conn, make_posting(
        gid="github_repo:gecko:ceb24bf1", company="Gecko Robotics",
        title="Embedded Software Engineer Intern - RoboSPLAT - Field Infra",
        locations=("Pittsburgh, PA",), url=url + "/application?embed=true",
    ), run) == "duplicate"


def test_a_url_without_an_id_falls_back_to_the_fuzzy_key_not_to_matching_everything(conn):
    """Two different jobs, neither URL carrying an id, must stay two jobs."""
    run = store.begin_run(conn)
    store.upsert_posting(conn, make_posting(
        gid="x:acme:1", title="Data Engineer", url="https://acme.test/careers"), run)
    assert store.upsert_posting(conn, make_posting(
        gid="x:acme:2", title="Platform Engineer", url="https://acme.test/careers"),
        run) == "new"


def test_the_same_id_at_two_employers_is_two_jobs(conn):
    """Req ids are only unique within an ATS tenant, so company stays part of the key."""
    run = store.begin_run(conn)
    url = "https://job-boards.greenhouse.io/{}/jobs/8204086"
    store.upsert_posting(conn, make_posting(
        gid="gh:acme:1", company="Acme", url=url.format("acme")), run)
    assert store.upsert_posting(conn, make_posting(
        gid="gh:other:1", company="Other Corp", url=url.format("other")), run) == "new"


def test_dedup_pass_folds_copies_that_predate_the_id_rule(conn):
    """The case that actually mattered on 2026-09-17.

    `upsert_posting` only consults a duplicate finder when it INSERTS. 17,000 postings were
    already in the ledger when the exact-id rule shipped, carrying no `ats_token` at all, so
    every existing copy took the "seen before" branch forever and the rule could never see
    them. Modelled here as two rows first stored from URLs with no id in them, then re-seen
    from their real ATS URLs on a later sweep.
    """
    url = "https://jobs.ashbyhq.com/acme/29ffbcea-17c3-4488-9d57-a9f7249b49a5"
    old = datetime.now(UTC) - timedelta(days=2)

    legacy = store.begin_run(conn)
    assert store.upsert_posting(conn, make_posting(
        gid="ashby:acme:29ffbcea", url="https://acme.test/careers",
        first_seen_at=old), legacy) == "new"
    assert store.upsert_posting(conn, make_posting(
        gid="github_repo:acme:d6076e1f", title="Software Engineering Co-op",
        locations=("New York City, NY",), url="https://acme.test/jobs",
        first_seen_at=old), legacy) == "new"

    # A later sweep re-lists both from their real ATS URLs; the update path backfills the
    # token, but neither row is an insert, so no finder runs.
    now = store.begin_run(conn)
    store.upsert_posting(conn, make_posting(
        gid="ashby:acme:29ffbcea", url=url, first_seen_at=old), now)
    store.upsert_posting(conn, make_posting(
        gid="github_repo:acme:d6076e1f", title="Software Engineering Co-op",
        locations=("New York City, NY",), url=url + "?utm_source=Simplify",
        first_seen_at=old), now)
    assert conn.execute("SELECT COUNT(*) FROM duplicates").fetchone()[0] == 0

    assert store.dedup_by_ats_token(conn, now) == 1
    survivors = [p.global_id for p in store.unalerted_postings(conn, now, "t")]
    assert survivors == ["ashby:acme:29ffbcea"]


def test_dedup_pass_is_idempotent_and_picks_a_stable_canonical(conn):
    """An unstable winner would make the pair re-alert every time the order shifted."""
    url = "https://jobs.ashbyhq.com/acme/29ffbcea-17c3-4488-9d57-a9f7249b49a5"
    run = store.begin_run(conn)
    older = datetime.now(UTC) - timedelta(days=3)
    store.upsert_posting(conn, make_posting(gid="b:acme:2", url=url), run)
    store.upsert_posting(conn, make_posting(
        gid="a:acme:1", url=url + "?x=1", title="Other Title", first_seen_at=older), run)
    first_pass = store.dedup_by_ats_token(conn, run)
    rows = conn.execute("SELECT global_id, matched_global_id FROM duplicates").fetchall()
    assert store.dedup_by_ats_token(conn, run) == 0, "second pass must write nothing"
    assert first_pass == 1
    assert [tuple(r) for r in rows] == [("b:acme:2", "a:acme:1")]


# --- 2. the still-open recap ------------------------------------------------------------

def seed_pool(conn, run, count=4, days_old=1):
    """`count` still-open postings, all seen in `run`."""
    when = datetime.now(UTC) - timedelta(days=days_old)
    out = []
    for i in range(count):
        posting = make_posting(
            gid=f"ashby:acme:{i}", title=f"Software Engineer {i}",
            locations=("New York",), url=f"https://jobs.ashbyhq.com/acme/job-{i}",
            first_seen_at=when,
        )
        store.upsert_posting(conn, posting, run)
        store.upsert_analysis(conn, make_analysis(gid=posting.global_id))
        out.append(posting)
    return out


def test_still_open_postings_only_returns_what_this_run_saw(conn):
    """`last_seen_run` is the project's only closed-req signal: a board that stops listing
    a posting is how it says the job is gone."""
    first = store.begin_run(conn)
    seed_pool(conn, first, count=2)
    second = store.begin_run(conn)
    since = datetime.now(UTC) - timedelta(days=3)
    assert store.still_open_postings(conn, second, since) == []
    assert len(store.still_open_postings(conn, first, since)) == 2


def test_still_open_postings_respects_the_day_window(conn):
    run = store.begin_run(conn)
    seed_pool(conn, run, count=2, days_old=10)
    since = datetime.now(UTC) - timedelta(days=3)
    assert store.still_open_postings(conn, run, since) == []


def test_recap_excludes_what_the_new_section_already_carries(conn, monkeypatch):
    """A posting must not appear twice in one email."""
    run = store.begin_run(conn)
    pool = seed_pool(conn, run, count=4)
    profile = make_profile(limits=Limits(min_score=0.0, recap_days=3))
    monkeypatch.setattr(runner, "_analyze",
                        lambda c, posting, cfg: make_analysis(gid=posting.global_id))
    recap = runner._build_recap(
        conn, profile, None, set(), set(), set(), run,
        exclude={pool[0].global_id, pool[1].global_id},
    )
    assert {c.global_id for c in recap} == {pool[2].global_id, pool[3].global_id}


def test_recap_days_zero_disables_the_section(conn, monkeypatch):
    run = store.begin_run(conn)
    seed_pool(conn, run, count=3)
    monkeypatch.setattr(runner, "_analyze",
                        lambda c, posting, cfg: make_analysis(gid=posting.global_id))
    profile = make_profile(limits=Limits(min_score=0.0, recap_days=0))
    assert runner._build_recap(
        conn, profile, None, set(), set(), set(), run, exclude=set()) == []


def test_recap_is_not_a_side_door_around_min_score(conn, monkeypatch):
    run = store.begin_run(conn)
    seed_pool(conn, run, count=3)
    monkeypatch.setattr(runner, "_analyze",
                        lambda c, posting, cfg: make_analysis(gid=posting.global_id))
    profile = make_profile(limits=Limits(min_score=0.99, recap_days=3))
    assert runner._build_recap(
        conn, profile, None, set(), set(), set(), run, exclude=set()) == []


def test_recap_is_not_a_side_door_around_the_policy_ceiling(conn, monkeypatch):
    """`priorities.never` is absolute at any score (T8.10) - including here."""
    run = store.begin_run(conn)
    seed_pool(conn, run, count=2)
    monkeypatch.setattr(
        runner, "_analyze",
        lambda c, posting, cfg: make_analysis(gid=posting.global_id, function=("quant",)),
    )
    profile = make_profile(
        limits=Limits(min_score=0.0, recap_days=3),
        roles=Roles(priorities=Priorities(
            order=["swe"], never=["quant"], sort_by="priority", weights={},
            resolved=rank_weights(["swe"], {}),
        )),
    )
    assert runner._build_recap(
        conn, profile, None, set(), set(), set(), run, exclude=set()) == []


def test_recap_items_are_never_marked_alerted(conn, monkeypatch):
    """The whole point of a standing list: it recurs until the req closes or you apply.

    Marking them would make each item appear exactly once, which is what the NEW section
    already does - and would silently consume postings the digest never showed.
    """
    run = store.begin_run(conn)
    pool = seed_pool(conn, run, count=3)
    monkeypatch.setattr(runner, "_analyze",
                        lambda c, posting, cfg: make_analysis(gid=posting.global_id))
    profile = make_profile(limits=Limits(min_score=0.0, recap_days=3))
    recap = runner._build_recap(
        conn, profile, None, set(), set(), set(), run, exclude=set())
    assert len(recap) == 3
    assert store.alerted_at(conn, pool[0].global_id, profile="t") is None


def test_recap_skips_postings_already_applied_to(conn, monkeypatch):
    run = store.begin_run(conn)
    pool = seed_pool(conn, run, count=3)
    monkeypatch.setattr(runner, "_analyze",
                        lambda c, posting, cfg: make_analysis(gid=posting.global_id))
    profile = make_profile(limits=Limits(min_score=0.0, recap_days=3))
    recap = runner._build_recap(
        conn, profile, None, set(), {pool[0].global_id}, set(), run, exclude=set())
    assert pool[0].global_id not in {c.global_id for c in recap}


# --- the digest that carries it ---------------------------------------------------------

class FakeCandidate:
    def __init__(self, gid, company, title, group="swe", score=0.5):
        self.posting = make_posting(gid=gid, company=company, title=title)
        self.analysis = make_analysis(gid=gid)
        self.score = score
        self.group = group
        self.global_id = gid


def capture(monkeypatch):
    sent = {}

    def fake(client, title, body, priority=3, click_url=None, html_body=None,
             channel="all"):
        sent.update(body=body, html=html_body, channel=channel)
        return True

    monkeypatch.setattr(notify, "_deliver", fake)
    return sent


def test_digest_renders_both_sections_and_states_the_recap_window(monkeypatch):
    sent = capture(monkeypatch)
    profile = make_profile(limits=Limits(recap_days=3, max_recap_items=25))
    notify.send_profile_digest(
        None, [FakeCandidate("a", "Acme", "New Grad SWE")], profile,
        recap=[FakeCandidate("b", "Other", "Open Req", group="mle")], recap_pool=55,
    )
    assert "NEW THIS SWEEP (1)" in sent["body"]
    assert "STILL OPEN - last 3 days (1 of 55)" in sent["body"]
    assert "MLE (1)" in sent["body"]
    assert "Other - Open Req" in sent["body"]


def test_a_quiet_sweep_still_mails_the_recap_rather_than_nothing(monkeypatch):
    """The bug as the reader experienced it: an hourly mail saying only 'no new openings'."""
    sent = capture(monkeypatch)
    profile = make_profile(limits=Limits(recap_days=3))
    notify.send_profile_digest(
        None, [], profile, recap=[FakeCandidate("b", "Other", "Open Req")], recap_pool=9)
    assert "NEW THIS SWEEP" not in sent["body"]
    assert "STILL OPEN" in sent["body"]
    assert "Other - Open Req" in sent["body"]


def test_only_a_genuinely_empty_sweep_says_so(monkeypatch):
    sent = capture(monkeypatch)
    notify.send_profile_digest(None, [], make_profile(), recap=[], recap_pool=0)
    assert "No new eligible openings" in sent["body"]


def test_recap_groups_follow_the_profiles_priority_order(monkeypatch):
    """Grouping must not re-sort: `sort_candidates` already applied the profile's sort_by."""
    sent = capture(monkeypatch)
    profile = make_profile(limits=Limits(recap_days=3))
    notify.send_profile_digest(
        None, [], profile, recap_pool=3,
        recap=[FakeCandidate("a", "A", "T1", group="mle"),
               FakeCandidate("b", "B", "T2", group="swe"),
               FakeCandidate("c", "C", "T3", group="mle")],
    )
    assert sent["body"].index("MLE (2)") < sent["body"].index("SWE (1)")


def test_digest_html_is_one_document_with_nested_headings(monkeypatch):
    sent = capture(monkeypatch)
    profile = make_profile(limits=Limits(recap_days=3))
    notify.send_profile_digest(
        None, [FakeCandidate("a", "Acme", "New Grad SWE")], profile,
        recap=[FakeCandidate("b", "Other", "Open Req")], recap_pool=2,
    )
    assert sent["html"].count("<html>") == 1
    assert sent["html"].count("</body></html>") == 1
    assert "<h1>Jobmon Opening Workflow</h1>" in sent["html"]


def test_run_gap_is_appended_to_the_digest_footer(monkeypatch):
    sent = capture(monkeypatch)
    notify.send_profile_digest(
        None, [FakeCandidate("a", "Acme", "T")], make_profile(),
        run_gap="Previous sweep was 4h 00m ago (run #42), longer than the ~1h schedule.",
    )
    assert "Previous sweep was 4h 00m ago" in sent["body"]
    assert "Previous sweep was 4h 00m ago" in sent["html"]


# --- the schedule -----------------------------------------------------------------------

def test_run_gap_is_silent_when_the_schedule_is_on_time():
    """An unconditional 'last run 58 minutes ago' is just a line to skim past."""
    recent = ledger.Health(
        last_run_id=7,
        last_run_at=(datetime.now(UTC) - timedelta(minutes=58)).isoformat(),
    )
    assert runner.describe_run_gap(recent) is None


def test_run_gap_reports_a_slipped_schedule():
    late = ledger.Health(
        last_run_id=33,
        last_run_at=(datetime.now(UTC) - timedelta(hours=5, minutes=21)).isoformat(),
    )
    message = runner.describe_run_gap(late)
    assert "5h 21m" in message
    assert "#33" in message


@pytest.mark.parametrize("health", [
    None,
    ledger.Health(),                                   # never run
    ledger.Health(last_run_at="not-a-timestamp"),      # corrupt stamp
])
def test_run_gap_says_nothing_when_it_cannot_know(health):
    assert runner.describe_run_gap(health) is None


def test_health_round_trips_the_run_stamp(tmp_path):
    path = tmp_path / "health.json"
    stamp = datetime.now(UTC).isoformat()
    ledger.save_health(ledger.Health(counts={"a": 1}, last_run_id=5, last_run_at=stamp),
                       path)
    assert ledger.load_health(path).last_run_at == stamp


def test_a_pre_existing_health_file_without_the_stamp_still_loads(tmp_path):
    """Forward compatibility: the deployed health.json predates `last_run_at`."""
    path = tmp_path / "health.json"
    path.write_text('{"counts": {"ats_public": 3}, "last_run_id": 33}', encoding="utf-8")
    health = ledger.load_health(path)
    assert health.last_run_id == 33 and health.last_run_at is None


# --- --min-interval: three cron attempts, one sweep --------------------------------------
#
# The workflow fires at :07, :27 and :47 because GitHub drops scheduled ticks under load and
# one entry gives that one chance to lose the hour. This guard is what stops three attempts
# becoming three digests.

def run_args(tmp_path, **overrides):
    import argparse
    args = argparse.Namespace(
        dry_run=False, seed=False, adapter=None, profile=None,
        ledger=tmp_path / "postings.jsonl", min_interval=None, db=":memory:", verbose=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def write_health(tmp_path, minutes_ago, run_id=33):
    stamp = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()
    ledger.save_health(ledger.Health(counts={}, last_run_id=run_id, last_run_at=stamp),
                       tmp_path / "health.json")


@pytest.mark.parametrize("value,expected_minutes", [
    ("50m", 50), ("2h", 120), ("1d", 1440), (" 90 m ", 90),
])
def test_parse_duration(value, expected_minutes):
    assert cli.parse_duration(value) == timedelta(minutes=expected_minutes)


@pytest.mark.parametrize("value", ["", "soon", "50", "m50", "-5m", "1w"])
def test_parse_duration_rejects_nonsense(value):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_duration(value)


def test_second_attempt_within_the_interval_skips_without_sweeping(tmp_path, monkeypatch,
                                                                   capsys):
    """Exit 0, not a dedicated code: from the workflow's side a skip IS success."""
    monkeypatch.setenv("JOBMON_NTFY_TOPIC", "t")
    monkeypatch.setattr(runner, "run_scheduled",
                        lambda **kw: pytest.fail("must not sweep"))
    write_health(tmp_path, minutes_ago=12)
    assert cli.cmd_run(run_args(tmp_path, min_interval="50m")) == cli.EXIT_OK
    assert "skipping" in capsys.readouterr().out


def test_attempt_after_the_interval_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBMON_NTFY_TOPIC", "t")
    swept = []
    monkeypatch.setattr(runner, "run_scheduled",
                        lambda **kw: swept.append(kw) or runner.RunResult())
    write_health(tmp_path, minutes_ago=51)
    assert cli.cmd_run(run_args(tmp_path, min_interval="50m")) == cli.EXIT_OK
    assert len(swept) == 1


def test_a_first_ever_run_is_never_skipped(tmp_path, monkeypatch):
    """No stamp means nothing is known, which must not read as 'ran a moment ago'."""
    monkeypatch.setenv("JOBMON_NTFY_TOPIC", "t")
    swept = []
    monkeypatch.setattr(runner, "run_scheduled",
                        lambda **kw: swept.append(kw) or runner.RunResult())
    assert cli.cmd_run(run_args(tmp_path, min_interval="50m")) == cli.EXIT_OK
    assert len(swept) == 1


def test_min_interval_without_a_ledger_is_a_misconfiguration(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBMON_NTFY_TOPIC", "t")
    args = run_args(tmp_path, min_interval="50m", ledger=None)
    assert cli.cmd_run(args) == cli.EXIT_MISCONFIGURED


def test_an_unparseable_min_interval_fails_loudly(tmp_path, monkeypatch):
    """Never silently fall back to 'run every time' - that is three digests an hour."""
    monkeypatch.setenv("JOBMON_NTFY_TOPIC", "t")
    monkeypatch.setattr(runner, "run_scheduled",
                        lambda **kw: pytest.fail("must not sweep"))
    write_health(tmp_path, minutes_ago=1)
    assert cli.cmd_run(run_args(tmp_path, min_interval="whenever")) == cli.EXIT_MISCONFIGURED
