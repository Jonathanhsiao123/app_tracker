"""Application tracking and the funnel report (spec 11, build step 7a).

`jobmon apply` and `jobmon log` are pure bookkeeping on postings the pipeline already
wrote; `funnel` is the one report that decides whether the volume strategy is working
(spec 11) - a wrong conversion rate is worse than no report at all, so the arithmetic
(T7.3, T7.4) gets the same care as the dedup and eligibility gates.
"""

from datetime import UTC, datetime

import pytest

from jobmon import cli, store
from jobmon.models import Analysis, Posting


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    yield c
    c.close()


def make_posting(gid: str, company: str = "Acme", title: str = "Software Engineer") -> Posting:
    now = datetime.now(UTC)
    return Posting(
        global_id=gid, source="greenhouse", company=company, title=title,
        locations=["NYC"], description_html="", description_text="",
        apply_url=f"https://boards.greenhouse.io/{company}/{gid}",
        first_seen_at=now, last_seen_at=now,
    )


def make_analysis(gid: str, function=("swe",), recommended_variant: str = "mle") -> Analysis:
    return Analysis(
        global_id=gid, role_class="new_grad", function=list(function),
        eligibility="unknown", eligibility_evidence=[], keywords={}, gaps=[],
        recommended_variant=recommended_variant, variant_scores={},
    )


def seed_posting(conn, gid: str, function=("swe",), variant: str = "mle") -> None:
    store.upsert_posting(conn, make_posting(gid))
    store.upsert_analysis(conn, make_analysis(gid, function=function, recommended_variant=variant))


# --- T7.1: unknown global_id --------------------------------------------------------

def test_apply_on_unknown_global_id_raises_clear_error(conn):
    with pytest.raises(store.ApplicationError, match="no posting"):
        store.record_application(conn, "greenhouse:nobody:1", "mle", datetime.now(UTC))


def test_apply_on_unknown_global_id_writes_nothing(conn):
    with pytest.raises(store.ApplicationError):
        store.record_application(conn, "greenhouse:nobody:1", "mle", datetime.now(UTC))
    assert conn.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"] == 0


def test_log_on_unknown_global_id_raises_clear_error(conn):
    with pytest.raises(store.ApplicationError, match="no posting"):
        store.record_event(conn, "greenhouse:nobody:1", "screen", datetime.now(UTC))


def test_log_without_a_prior_apply_raises(conn):
    seed_posting(conn, "greenhouse:acme:1")
    with pytest.raises(store.ApplicationError, match="apply"):
        store.record_event(conn, "greenhouse:acme:1", "screen", datetime.now(UTC))


# --- T7.2: apply twice never double-counts ------------------------------------------

def test_apply_twice_refuses_the_second_and_does_not_double_count(conn):
    seed_posting(conn, "greenhouse:acme:1")
    store.record_application(conn, "greenhouse:acme:1", "mle", datetime.now(UTC))
    with pytest.raises(store.ApplicationError, match="already marked applied"):
        store.record_application(conn, "greenhouse:acme:1", "systems", datetime.now(UTC))
    assert conn.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"] == 1


def test_second_apply_does_not_overwrite_the_first_variant(conn):
    """The refusal must be a true no-op, not a partial write that clobbers the original
    row before raising."""
    seed_posting(conn, "greenhouse:acme:1")
    store.record_application(conn, "greenhouse:acme:1", "mle", datetime.now(UTC))
    with pytest.raises(store.ApplicationError):
        store.record_application(conn, "greenhouse:acme:1", "systems", datetime.now(UTC))
    row = conn.execute(
        "SELECT variant FROM applications WHERE global_id = ?", ("greenhouse:acme:1",)
    ).fetchone()
    assert row["variant"] == "mle"


# --- T7.3: funnel with zero applications --------------------------------------------

def test_funnel_with_zero_applications_renders_empty_report(conn):
    report = store.funnel_report(conn)
    assert report["applications_sent"] == 0
    assert report["callbacks"] == 0
    assert report["conversion_rate"] == 0.0
    assert report["by_variant"] == {}
    assert report["by_function"] == {}


# --- T7.4: dummy data, 40 applied / 5 screens ---------------------------------------

def test_funnel_conversion_rate_matches_dummy_data(conn):
    for i in range(40):
        gid = f"greenhouse:acme:{i}"
        variant = "mle" if i < 20 else "systems"
        seed_posting(conn, gid, function=("swe",), variant=variant)
        store.record_application(conn, gid, variant, datetime.now(UTC))
    for i in range(5):
        store.record_event(conn, f"greenhouse:acme:{i}", "screen", datetime.now(UTC))

    report = store.funnel_report(conn)
    assert report["applications_sent"] == 40
    assert report["callbacks"] == 5
    assert report["conversion_rate"] == 12.5
    # all 5 screens landed on "mle" postings (i < 20)
    assert report["by_variant"]["mle"] == {"applied": 20, "callbacks": 5, "conversion_rate": 25.0}
    assert report["by_variant"]["systems"] == {"applied": 20, "callbacks": 0, "conversion_rate": 0.0}


def test_funnel_counts_a_posting_once_even_with_multiple_callback_events(conn):
    """Screen then onsite for one posting is one callback, not two - `funnel` counts
    distinct postings that ever reached a callback status, not raw event rows."""
    seed_posting(conn, "greenhouse:acme:1")
    store.record_application(conn, "greenhouse:acme:1", "mle", datetime.now(UTC))
    store.record_event(conn, "greenhouse:acme:1", "screen", datetime.now(UTC))
    store.record_event(conn, "greenhouse:acme:1", "onsite", datetime.now(UTC))
    report = store.funnel_report(conn)
    assert report["callbacks"] == 1


def test_reject_alone_is_not_a_callback(conn):
    seed_posting(conn, "greenhouse:acme:1")
    store.record_application(conn, "greenhouse:acme:1", "mle", datetime.now(UTC))
    store.record_event(conn, "greenhouse:acme:1", "reject", datetime.now(UTC))
    report = store.funnel_report(conn)
    assert report["callbacks"] == 0
    assert report["by_variant"]["mle"]["callbacks"] == 0


def test_funnel_splits_by_function_and_multi_label_postings_count_in_both(conn):
    seed_posting(conn, "greenhouse:acme:1", function=("swe", "security"))
    store.record_application(conn, "greenhouse:acme:1", "security", datetime.now(UTC))
    report = store.funnel_report(conn)
    assert report["by_function"]["swe"]["applied"] == 1
    assert report["by_function"]["security"]["applied"] == 1


# --- CLI-level: exit codes -----------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def test_cli_apply_on_unknown_global_id_exits_nonzero(db_path, capsys):
    assert cli.main(["--db", db_path, "init-db"]) == 0
    code = cli.main(["--db", db_path, "apply", "greenhouse:nobody:1", "--variant", "mle"])
    assert code != 0
    assert "no posting" in capsys.readouterr().err


def test_cli_apply_then_apply_again_exits_nonzero(db_path, capsys):
    cli.main(["--db", db_path, "init-db"])
    with store.connect(db_path) as conn:
        store.init_db(conn)
        seed_posting(conn, "greenhouse:acme:1")
    assert cli.main(["--db", db_path, "apply", "greenhouse:acme:1", "--variant", "mle"]) == 0
    code = cli.main(["--db", db_path, "apply", "greenhouse:acme:1", "--variant", "mle"])
    assert code != 0
    assert "already marked applied" in capsys.readouterr().err


def test_cli_funnel_on_empty_db_does_not_crash(db_path, capsys):
    cli.main(["--db", db_path, "init-db"])
    assert cli.main(["--db", db_path, "funnel"]) == 0
    assert "applications sent: 0" in capsys.readouterr().out
