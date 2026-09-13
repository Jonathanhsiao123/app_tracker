"""Testbench T3c - normalization (build step 3).

Payload fixtures are trimmed copies of live responses captured 2026-09-06.
"""

from datetime import UTC, datetime, timedelta

import pytest

from jobmon import normalize
from jobmon.models import Posting

# --- T3c: required fields fail at the adapter, not at notify -----------------------

def test_missing_required_field_raises_at_the_adapter():
    with pytest.raises(KeyError):
        normalize.from_greenhouse({"id": 1, "content": "<p>x</p>"}, "Acme")  # no title


def test_missing_apply_url_raises_at_the_adapter():
    with pytest.raises(KeyError):
        normalize.from_lever({"id": "1", "text": "SWE"}, "Acme")  # no hostedUrl


def test_normalize_returns_none_rather_than_propagating():
    """The dispatcher swallows the adapter's error and logs it, so one malformed payload
    does not abort a 169-board sweep."""
    assert normalize.normalize({"_ats": "greenhouse", "_company": "Acme", "id": 1}) is None


def test_unknown_source_returns_none():
    assert normalize.normalize({"_ats": "workday", "_company": "Acme"}) is None


def test_locale_is_never_silently_absent():
    p = normalize.from_greenhouse(
        {"id": 1, "title": "SWE", "content": "<p>x</p>", "absolute_url": "https://x/1"}, "Acme"
    )
    assert p.locale == "US"


def test_raw_is_stored_untouched_including_nested_junk():
    junk = {"weird": {"nested": [1, {"deep": None}]}, "unexpected": True}
    payload = {"id": 1, "title": "SWE", "content": "<p>x</p>",
               "absolute_url": "https://x/1", **junk}
    p = normalize.from_greenhouse(payload, "Acme")
    assert p.raw["weird"] == junk["weird"]
    assert p.raw["unexpected"] is True


# --- parsers, against live payload shapes ------------------------------------------

def test_from_greenhouse():
    p = normalize.from_greenhouse(
        {
            "id": 4730836005,
            "title": "Software Engineer, New Grad",
            "content": "&lt;p&gt;We build things.&lt;/p&gt;",  # greenhouse escapes it
            "absolute_url": "https://job-boards.greenhouse.io/scaleai/jobs/4730836005",
            "location": {"name": "San Francisco, CA"},
            "updated_at": "2026-09-01T12:00:00-04:00",
        },
        "Scale AI",
    )
    assert p.global_id == "greenhouse:scale-ai:4730836005"
    assert p.locations == ["San Francisco, CA"]
    assert p.description_text == "We build things."  # unescaped, then stripped
    assert p.posted_at.tzinfo is not None


def test_from_lever_includes_the_lists_blocks():
    """The requirements - and the sponsorship language - live in `lists`, not in
    `description`. Dropping them would blind the eligibility filter."""
    p = normalize.from_lever(
        {
            "id": "abc-123",
            "text": "Software Engineer, New Grad",
            "description": "<p>About us.</p>",
            "lists": [{"text": "Requirements",
                       "content": "<li>We are unable to sponsor visas.</li>"}],
            "hostedUrl": "https://jobs.lever.co/palantir/abc-123",
            "createdAt": 1756000000000,  # epoch ms
            "categories": {"location": "NYC", "allLocations": ["NYC", "Seattle"]},
        },
        "Palantir",
    )
    assert p.locations == ["NYC", "Seattle"]
    assert "unable to sponsor" in p.description_text
    assert p.posted_at.year == 2025


def test_from_ashby():
    p = normalize.from_ashby(
        {
            "id": "uuid-1",
            "title": "SWE Intern",
            "descriptionHtml": "<p>Work here.</p>",
            "jobUrl": "https://jobs.ashbyhq.com/notion/uuid-1",
            "location": "NYC",
            "secondaryLocations": [{"location": "SF"}],
            "isRemote": True,
            "publishedAt": "2026-08-15T00:00:00Z",
        },
        "Notion",
    )
    assert p.locations == ["NYC", "SF"]
    assert p.remote_type == "remote"


WORKABLE_JOB = {
    "shortcode": "CA15908E0A",
    "code": "",  # present on every job and almost always empty - NOT the id
    "title": "Software Engineer Intern",
    "description": "<p>Build.</p>",
    "requirements": "<p>Must be enrolled.</p>",
    "application_url": "https://apply.workable.com/enfos-inc/j/CA15908E0A",
    "url": "https://apply.workable.com/j/CA15908E0A",
    "city": "Durham", "country": "United States",
    "created_at": "2026-09-01T00:00:00Z",
}


def test_from_workable_uses_shortcode_not_code():
    """Regression: `code` is present on every Workable job and is almost always the empty
    string. Reading the key list without the values makes it look like the identifier, and
    using it drops every Workable posting with a "required field missing" error."""
    p = normalize.from_workable(WORKABLE_JOB, "ENFOS")
    assert p.global_id == "workable:enfos:CA15908E0A"
    assert p.locations == ["Durham, United States"]
    assert "Must be enrolled" in p.description_text


def test_empty_code_field_does_not_become_the_id():
    p = normalize.from_workable({**WORKABLE_JOB, "code": ""}, "ENFOS")
    assert p.global_id.endswith("CA15908E0A")


def test_workable_without_a_shortcode_raises():
    with pytest.raises(KeyError, match="shortcode"):
        normalize.from_workable({k: v for k, v in WORKABLE_JOB.items() if k != "shortcode"},
                                "ENFOS")


def test_smartrecruiters_is_explicitly_out_of_scope():
    with pytest.raises(NotImplementedError, match="out of scope"):
        normalize.from_smartrecruiters({}, "Acme")


# --- html_to_text: sentence boundaries must survive --------------------------------

def test_block_tags_become_line_breaks():
    """A stripper that runs "...cannot sponsor.Applicants..." together produces garbage
    evidence, and evidence is the one output read verbatim."""
    text = normalize.html_to_text("<p>We cannot sponsor.</p><p>Applicants must apply.</p>")
    assert "sponsor.\n" in text or "sponsor.\n\n" in text
    assert "sponsor.Applicants" not in text


def test_list_items_become_bullets():
    text = normalize.html_to_text("<ul><li>Python</li><li>Rust</li></ul>")
    assert "- Python" in text and "- Rust" in text


def test_script_and_style_are_dropped():
    text = normalize.html_to_text("<p>Real</p><script>var x=1;</script><style>p{}</style>")
    assert "var x" not in text and "p{}" not in text
    assert "Real" in text


def test_entities_are_unescaped():
    assert "R&D" in normalize.html_to_text("<p>R&amp;D team</p>")


def test_empty_html_is_empty_string():
    assert normalize.html_to_text("") == ""


# --- normalize_title / normalize_company -------------------------------------------

@pytest.mark.parametrize(
    "a,b",
    [
        ("Software Engineer, New Grad (REQ-1234)", "Software Engineer New Grad"),
        ("SWE Intern - Summer 2027", "SWE Intern"),
        ("Software Engineer II", "software engineer ii"),
    ],
)
def test_normalize_title_collapses_noise(a, b):
    assert normalize.normalize_title(a) == normalize.normalize_title(b)


@pytest.mark.parametrize(
    "name", ["Anduril", "Anduril Industries, Inc.", "ANDURIL INDUSTRIES", "Anduril Industries"]
)
def test_normalize_company_strips_legal_suffixes(name):
    '''An override keyed "Anduril" must match a posting from "Anduril Industries, Inc.".'''
    assert normalize.normalize_company(name).startswith("anduril")


def test_normalize_company_matches_across_suffixes():
    assert normalize.normalize_company("Acme, Inc.") == normalize.normalize_company("ACME")


# --- parse_datetime ----------------------------------------------------------------

@pytest.mark.parametrize(
    "value,year",
    [
        ("2026-09-01T12:00:00Z", 2026),
        ("2026-09-01T12:00:00-04:00", 2026),
        (1756000000, 2025),        # epoch seconds
        (1756000000000, 2025),     # epoch milliseconds
        ("1756000000000", 2025),
    ],
)
def test_parse_datetime_forms(value, year):
    parsed = normalize.parse_datetime(value)
    assert parsed.year == year
    assert parsed.tzinfo is not None  # naive datetimes break the 60-day dedup window


@pytest.mark.parametrize("value", [None, "", "not a date"])
def test_parse_datetime_rejects_junk(value):
    assert normalize.parse_datetime(value) is None


def test_naive_datetime_is_made_utc():
    naive = datetime(2026, 9, 1)  # noqa: DTZ001 - a naive input is the point of the test
    assert normalize.parse_datetime(naive).tzinfo is UTC


# --- parse_remote_type: never guess ------------------------------------------------

@pytest.mark.parametrize(
    "text,locations,expected",
    [
        ("This is a hybrid role.", [], "hybrid"),
        ("Fully remote position.", [], "remote"),
        ("This role is onsite in NYC.", [], "onsite"),
        ("We build distributed systems.", ["NYC"], None),
        ("", [], None),
    ],
)
def test_parse_remote_type(text, locations, expected):
    assert normalize.parse_remote_type(text, locations) == expected


# --- global_id ---------------------------------------------------------------------

def test_global_id_is_stable_across_company_spellings():
    assert Posting.make_global_id("greenhouse", "Acme, Inc.", "1") == \
           Posting.make_global_id("greenhouse", "ACME", "1")


# --- from_github_repo (build step 7b) -----------------------------------------------

def _repo_raw(**overrides):
    raw = {
        "company": "Acme",
        "title": "Software Engineer Intern",
        "locations": ["NYC"],
        "apply_url": "https://jobs.lever.co/acme/1",
        "posted_text": "2d",
    }
    raw.update(overrides)
    return raw


def test_from_github_repo_builds_a_posting_with_no_description():
    """These table rows carry no body at all (spec 5c) - eligibility must land on
    `unknown`, never `blocked`, because there is no text to match a blocking phrase in."""
    posting = normalize.from_github_repo(_repo_raw(), "Acme")
    assert posting.source == "github_repo"
    assert posting.description_html == ""
    assert posting.description_text == ""
    assert posting.locations == ["NYC"]
    assert posting.apply_url == "https://jobs.lever.co/acme/1"


def test_from_github_repo_job_id_is_a_stable_hash_of_apply_url():
    """No natural id exists for a table row. The hash must be the same across two rows
    that share one apply_url (a repost with an edited title) and different across two
    different apply_urls, or a run-over-run title edit would look like a brand new posting
    and re-alert."""
    a = normalize.from_github_repo(_repo_raw(title="Old Title"), "Acme")
    b = normalize.from_github_repo(_repo_raw(title="New Title"), "Acme")
    assert a.global_id == b.global_id
    c = normalize.from_github_repo(_repo_raw(apply_url="https://jobs.lever.co/acme/2"), "Acme")
    assert c.global_id != a.global_id


def test_from_github_repo_requires_apply_url():
    with pytest.raises(KeyError):
        normalize.from_github_repo(_repo_raw(apply_url=""), "Acme")


def test_from_github_repo_requires_title():
    with pytest.raises(KeyError):
        normalize.from_github_repo(_repo_raw(title=""), "Acme")


def test_from_github_repo_dispatches_through_normalize():
    raw = {**_repo_raw(), "_ats": "github_repo", "_company": "Acme"}
    posting = normalize.normalize(raw)
    assert posting is not None
    assert posting.source == "github_repo"


# --- parse_repo_date -----------------------------------------------------------------

def test_parse_repo_date_relative_day_count():
    result = normalize.parse_repo_date("3d")
    expected = datetime.now(UTC) - timedelta(days=3)
    assert abs((result - expected).total_seconds()) < 5


def test_parse_repo_date_absolute_month_day_uses_current_year():
    now = datetime.now(UTC)
    result = normalize.parse_repo_date(now.strftime("%b %d"))
    assert (result.month, result.day) == (now.month, now.day)


def test_parse_repo_date_rolls_back_a_year_when_the_bare_date_would_be_in_the_future():
    """"Mon DD" carries no year. A date that would land in the future if read against the
    current year must mean last year instead - the source only writes these once the
    posting already exists."""
    future = datetime.now(UTC) + timedelta(days=10)
    result = normalize.parse_repo_date(future.strftime("%b %d"))
    assert result.year == future.year - 1


def test_parse_repo_date_unrecognized_format_returns_none():
    assert normalize.parse_repo_date("sometime last week") is None


def test_parse_repo_date_none_input_returns_none():
    assert normalize.parse_repo_date(None) is None
