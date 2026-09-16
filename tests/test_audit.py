"""`jobmon audit --days 7` (addendum 1).

Measure before you filter. What this file actually asserts is that the report reads the
ALERTS table - what was sent - and not the postings table, and that its diagnosis points at
the right one of the three levers for a given distribution.
"""

from datetime import UTC, datetime, timedelta

import pytest
from test_scoring import make_analysis, make_posting

from jobmon import audit, store


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    yield c
    c.close()


def seed(conn, gid, company="Acme", source="greenhouse", role_class="new_grad",
         function=("swe",), eligibility="unknown", profile="jonathan",
         channel="digest", days_ago=0, alerted=True):
    posting = make_posting(gid=gid, company=company)
    posting.source = source
    store.upsert_posting(conn, posting, run_id=1)
    store.upsert_analysis(conn, make_analysis(
        gid=gid, role_class=role_class, function=function, eligibility=eligibility))
    if alerted:
        store.mark_alerted(conn, [gid], datetime.now(UTC) - timedelta(days=days_ago),
                           profile=profile, channel=channel)
    return posting


def test_audit_counts_what_was_sent_not_what_was_stored(conn):
    seed(conn, "gh:a:1", alerted=True)
    seed(conn, "gh:a:2", alerted=False)  # stored, never delivered
    report = audit.collect(conn, days=7)
    assert report.alerts_sent == 1


def test_audit_breaks_down_by_every_axis_the_addendum_names(conn):
    seed(conn, "gh:a:1", source="greenhouse", function=("swe",), role_class="new_grad")
    seed(conn, "lv:a:2", source="lever", function=("data",), role_class="unclear")
    seed(conn, "as:a:3", company="Beta", source="ashby", function=("mle", "swe"),
         eligibility="likely_ok", channel="instant")

    report = audit.collect(conn, days=7)
    assert report.alerts_sent == 3
    assert report.by_source == {"greenhouse": 1, "lever": 1, "ashby": 1}
    assert report.by_company == {"Acme": 2, "Beta": 1}
    assert report.by_role_class == {"new_grad": 2, "unclear": 1}
    assert report.by_eligibility == {"unknown": 2, "likely_ok": 1}
    assert report.by_channel == {"digest": 2, "instant": 1}
    # multi-label: one posting counts toward both of its functions
    assert report.by_function == {"swe": 2, "data": 1, "mle": 1}


def test_audit_respects_the_window(conn):
    seed(conn, "gh:a:1", days_ago=1)
    seed(conn, "gh:a:2", days_ago=30)
    assert audit.collect(conn, days=7).alerts_sent == 1
    assert audit.collect(conn, days=60).alerts_sent == 2


def test_audit_can_be_scoped_to_one_profile(conn):
    seed(conn, "gh:a:1", profile="jonathan")
    seed(conn, "gh:a:2", profile="someone-else")
    assert audit.collect(conn, days=7).alerts_sent == 2
    assert audit.collect(conn, days=7, profile="jonathan").alerts_sent == 1


def test_alert_to_apply_rate_counts_only_applications_against_alerted_postings(conn):
    for i in range(10):
        seed(conn, f"gh:a:{i}")
    store.record_application(conn, "gh:a:0", "generalist", datetime.now(UTC))
    report = audit.collect(conn, days=7)
    assert report.applied == 1
    assert report.apply_rate == 10.0


# --- the diagnosis: which of the three levers matters --------------------------------------

def test_unclear_dominating_role_class_points_at_on_unclear_role(conn):
    for i in range(10):
        seed(conn, f"gh:a:{i}", role_class="unclear" if i < 8 else "new_grad")
    findings = audit.diagnose(audit.collect(conn, days=7))
    assert any("on_unclear_role: digest" in f for f in findings)


def test_unknown_dominating_eligibility_points_at_on_unknown(conn):
    for i in range(10):
        seed(conn, f"gh:a:{i}", eligibility="unknown" if i < 9 else "likely_ok")
    findings = audit.diagnose(audit.collect(conn, days=7))
    assert any("on_unknown: digest" in f for f in findings)
    assert any("largest noise cut" in f for f in findings)


def test_a_few_companies_producing_most_alerts_points_at_the_company_list(conn):
    for i in range(20):
        seed(conn, f"gh:a:{i}", company="Flood Corp" if i < 15 else f"Small {i}")
    findings = audit.diagnose(audit.collect(conn, days=7))
    assert any("max_per_company_per_day" in f for f in findings)
    assert any("Flood Corp" in f for f in findings)


def test_a_low_apply_rate_points_at_the_threshold_not_the_sources(conn):
    for i in range(50):
        seed(conn, f"gh:a:{i}", role_class="new_grad", eligibility="likely_ok",
             company=f"Co {i}")
    findings = audit.diagnose(audit.collect(conn, days=7))
    assert any("limits.min_score" in f for f in findings)
    assert any("not the sources" in f for f in findings)


def test_a_healthy_distribution_falls_back_to_the_caps(conn):
    for i in range(20):
        seed(conn, f"gh:a:{i}", company=f"Co {i}", role_class="new_grad",
             eligibility="likely_ok", function=("mle",))
    for i in range(5):
        store.record_application(conn, f"gh:a:{i}", "mle", datetime.now(UTC))
    findings = audit.diagnose(audit.collect(conn, days=7))
    assert any("caps in `limits`" in f for f in findings)


def test_render_prints_the_numbers_and_the_reading_of_them(conn):
    for i in range(10):
        seed(conn, f"gh:a:{i}", role_class="unclear")
    text = audit.render(audit.collect(conn, days=7))
    assert "alerts sent            10" in text
    assert "by role_class" in text
    assert "alert-to-apply rate    0.0%" in text
    assert "Read it this way:" in text


def test_render_says_something_useful_when_nothing_was_sent(conn):
    text = audit.render(audit.collect(conn, days=7))
    assert "alerts sent            0" in text
    assert "jobmon health" in text


def test_an_unanalyzed_alert_counts_but_is_not_evidence_about_the_classifiers(conn):
    """A posting restored from the ledger has no analysis row until it is seen again.

    Skipping it would stop the buckets summing to `alerts_sent`, which is the first thing
    you check. But folding it into `unclear` is worse: the report would then confidently
    accuse `filters/role.py` of passing everything it cannot classify, when the rows simply
    are not there - and the whole point of this command is to pick the right lever.
    """
    posting = make_posting(gid="gh:a:1")
    store.upsert_posting(conn, posting, run_id=1)
    store.mark_alerted(conn, ["gh:a:1"], datetime.now(UTC), profile="p")
    report = audit.collect(conn, days=7)
    assert report.alerts_sent == 1
    assert sum(report.by_role_class.values()) == 1, "the columns still sum"
    assert report.by_role_class["not analyzed"] == 1
    assert report.by_role_class["unclear"] == 0
    assert report.unanalyzed == 1


def test_unanalyzed_alerts_do_not_trigger_the_classifier_diagnoses(conn):
    """The case a rebuilt cache produces: 100% unanalyzed. It must say so, and must NOT
    recommend `on_unclear_role` or `on_unknown` off the back of it."""
    for i in range(20):
        posting = make_posting(gid=f"gh:a:{i}", company=f"Co {i}")
        store.upsert_posting(conn, posting, run_id=1)
        store.mark_alerted(conn, [posting.global_id], datetime.now(UTC), profile="p")

    findings = audit.diagnose(audit.collect(conn, days=7))
    assert any("no analysis row" in f for f in findings)
    assert any("jobmon reanalyze" in f for f in findings)
    assert not any("on_unclear_role" in f for f in findings)
    assert not any("on_unknown" in f for f in findings)
    assert not any("function classifier is loose" in f for f in findings)


def test_a_partly_analyzed_window_judges_only_the_analyzed_rows(conn):
    """8 of 10 analyzed rows are `unclear`, alongside 40 unanalyzed ones. The reading is
    about the 10, not diluted to 8/50 by rows that were never classified."""
    for i in range(10):
        seed(conn, f"gh:x:{i}", company=f"Co {i}",
             role_class="unclear" if i < 8 else "new_grad")
    for i in range(40):
        posting = make_posting(gid=f"gh:y:{i}", company=f"Other {i}")
        store.upsert_posting(conn, posting, run_id=1)
        store.mark_alerted(conn, [posting.global_id], datetime.now(UTC), profile="p")

    report = audit.collect(conn, days=7)
    assert report.unanalyzed == 40
    findings = audit.diagnose(report)
    assert any("on_unclear_role: digest" in f for f in findings)
