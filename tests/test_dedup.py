"""Testbench T3 - dedup (build step 3, the important one).

The gate: **run 1 alerts N, run 2 alerts 0.** If that fails, nothing downstream is worth
building. Everything here runs against an in-memory SQLite; no network, no fixtures on disk.
"""

from datetime import UTC, datetime, timedelta

import pytest

from jobmon import store
from jobmon.models import Posting


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    yield c
    c.close()


def make(company="Acme", title="Software Engineer, New Grad", job_id="1",
         location="NYC", source="greenhouse", body="Build things.", at=None):
    at = at or datetime.now(UTC)
    return Posting(
        global_id=Posting.make_global_id(source, company, job_id),
        source=source,
        company=company,
        title=title,
        locations=[location] if location else [],
        description_html=f"<p>{body}</p>",
        description_text=body,
        apply_url=f"https://example.com/{job_id}",
        first_seen_at=at,
        last_seen_at=at,
    )


def run(conn, postings):
    """One full run. Returns the outcome per posting, in order."""
    run_id = store.begin_run(conn)
    return [store.upsert_posting(conn, p, run_id) for p in postings]


def distinct(n):
    """N genuinely different reqs. NOT `make(job_id=i)` in a loop: those share a company,
    title and location, so the fuzzy rule correctly collapses them into one alert."""
    return [make(job_id=str(i), title=f"Software Engineer, New Grad {chr(65 + i)}")
            for i in range(n)]


def reposted_count(conn, global_id):
    """`reposted_count` is storage state, not part of spec 4's Posting schema."""
    return conn.execute(
        "SELECT reposted_count FROM postings WHERE global_id = ?", (global_id,)
    ).fetchone()["reposted_count"]


# --- T3.1 / T3.2 / T3.3: the gate --------------------------------------------------

def test_run1_alerts_n_run2_alerts_zero(conn):
    batch = distinct(5)
    assert run(conn, batch).count("new") == 5           # T3.1
    assert run(conn, batch).count("new") == 0           # T3.2 - THE GATE
    assert run(conn, batch).count("new") == 0           # T3.3


def test_run2_advances_last_seen_at(conn):
    p = make()
    run(conn, [p])
    first = store.get_posting(conn, p.global_id).last_seen_at
    later = make(at=datetime.now(UTC) + timedelta(hours=3))
    run(conn, [later])
    assert store.get_posting(conn, p.global_id).last_seen_at > first


def test_all_n_are_stored(conn):
    run(conn, distinct(5))
    assert len(list(store.iter_postings(conn))) == 5


# --- T3.4: same global_id, description edited --------------------------------------

def test_edited_description_updates_without_alerting(conn):
    run(conn, [make(body="Original text.")])
    outcomes = run(conn, [make(body="Rewritten text with sponsorship details.")])
    assert outcomes == ["seen"]
    stored = store.get_posting(conn, make().global_id)
    assert stored.description_text == "Rewritten text with sponsorship details."


# --- T3.5: fuzzy duplicate, new global_id ------------------------------------------

def test_same_company_title_location_new_id_is_a_duplicate(conn):
    run(conn, [make(job_id="1")])
    outcomes = run(conn, [make(job_id="2")])  # reposted under a new req id
    assert outcomes == ["duplicate"]
    dupes = conn.execute("SELECT * FROM duplicates").fetchall()
    assert len(dupes) == 1
    assert dupes[0]["matched_global_id"] == Posting.make_global_id("greenhouse", "Acme", "1")


def test_duplicate_is_still_stored(conn):
    """Nothing is ever deleted - the duplicate is visible so churn can be seen."""
    run(conn, [make(job_id="1")])
    run(conn, [make(job_id="2")])
    assert store.get_posting(conn, Posting.make_global_id("greenhouse", "Acme", "2")) is not None


def test_fuzzy_match_expires_after_the_window(conn):
    old = datetime.now(UTC) - timedelta(days=90)
    run(conn, [make(job_id="1", at=old)])
    assert run(conn, [make(job_id="2")]) == ["new"]  # outside 60 days: a genuine new req


def test_req_id_and_cycle_year_do_not_defeat_the_fuzzy_key(conn):
    run(conn, [make(job_id="1", title="Software Engineer, New Grad (REQ-1234)")])
    outcomes = run(conn, [make(job_id="2", title="Software Engineer, New Grad (REQ-9876)")])
    assert outcomes == ["duplicate"]


# --- T3.6: disappeared, then returned ----------------------------------------------

def test_repost_bumps_reposted_count(conn):
    p = make()
    run(conn, [p])            # run 1: present
    run(conn, [])             # run 2: gone
    outcomes = run(conn, [p])  # run 3: back
    assert outcomes == ["repost"]
    assert reposted_count(conn, p.global_id) == 1


def test_repost_does_not_alert(conn):
    p = make()
    run(conn, [p])
    run(conn, [])
    assert "new" not in run(conn, [p])


def test_continuously_present_posting_is_never_a_repost(conn):
    p = make()
    for _ in range(4):
        run(conn, [p])
    assert reposted_count(conn, p.global_id) == 0


# --- T3.7: fuzzy match must not cross companies ------------------------------------

def test_same_title_at_two_companies_both_alert(conn):
    outcomes = run(conn, [
        make(company="Acme", job_id="1"),
        make(company="Globex", job_id="1"),
    ])
    assert outcomes == ["new", "new"]
    assert conn.execute("SELECT COUNT(*) c FROM duplicates").fetchone()["c"] == 0


def test_company_legal_suffix_does_not_split_one_company(conn):
    """An override keyed "Acme" must match a posting from "Acme, Inc." (spec 6)."""
    run(conn, [make(company="Acme", job_id="1")])
    assert run(conn, [make(company="Acme, Inc.", job_id="2")]) == ["duplicate"]


# --- mark_alerted ------------------------------------------------------------------

def test_mark_alerted_is_written_after_delivery(conn):
    p = make()
    run(conn, [p])
    assert store.get_posting(conn, p.global_id) is not None
    assert conn.execute(
        "SELECT alerted_at FROM postings WHERE global_id = ?", (p.global_id,)
    ).fetchone()["alerted_at"] is None
    store.mark_alerted(conn, [p.global_id], datetime.now(UTC))
    assert conn.execute(
        "SELECT alerted_at FROM postings WHERE global_id = ?", (p.global_id,)
    ).fetchone()["alerted_at"] is not None
