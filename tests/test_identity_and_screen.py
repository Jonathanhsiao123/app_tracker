"""Cross-source job identity, title-first classification, and the clearance screen."""

from datetime import UTC, datetime, timedelta

import pytest

from jobmon import runner, store
from jobmon.analyze import screen
from jobmon.filters import role
from jobmon.models import Posting
from jobmon.normalize import canonical_url, location_city, url_identity
from jobmon.profiles import Profile, ProfileError, parse


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    yield c
    c.close()


def posting(gid, company="Acme", title="Software Engineer Intern", url=None,
            locations=("New York, NY",), first_seen_at=None, body=""):
    when = first_seen_at or datetime.now(UTC)
    return Posting(
        global_id=gid, source=gid.split(":")[0], company=company, title=title,
        locations=list(locations), description_html="", description_text=body,
        apply_url=url or f"https://example.test/{gid}", first_seen_at=when,
        last_seen_at=when, posted_at=when,
    )


# --- identity ---------------------------------------------------------------------------

def test_canonical_url_ignores_scheme_www_slash_and_tracking():
    a = "http://www.aerotech.applytojob.com/apply/OFIYSpO0HW/Intern/"
    b = ("https://aerotech.applytojob.com/apply/OFIYSpO0HW/Intern"
         "?utm_source=Simplify&ref=Simplify")
    assert canonical_url(a) == canonical_url(b)


def test_canonical_url_keeps_the_query_that_names_the_job():
    url = "https://careers.duolingo.com/jobs/8805925002?gh_jid=8805925002&utm_source=X"
    assert canonical_url(url) == "https://careers.duolingo.com/jobs/8805925002?gh_jid=8805925002"


@pytest.mark.parametrize("url", [
    "https://acme.com/careers", "https://acme.com/jobs/", "https://acme.com/careers/2027",
])
def test_a_generic_careers_page_is_not_a_job_identity(url):
    assert url_identity(url) is None


@pytest.mark.parametrize("url", [
    "https://ragle.applytojob.com/apply/lonTfWhOqm/Software-Engineer-Intern",
    "https://avav.wd1.myworkdayjobs.com/en-US/avav/job/Melbourne-FL/SWE-Intern_8550",
    "https://jobs.ashbyhq.com/acme/1e8d195c-e28e-423d-a06a-9eeb401ed4fc",
])
def test_a_specific_job_link_is_an_identity(url):
    assert url_identity(url) is not None


def test_location_city_matches_across_spellings():
    assert location_city("Quincy, Massachusetts, United States") == location_city("Quincy, MA")
    assert location_city("Atlanta, GA +8") == "atlanta"


def _live(conn, *postings):
    run = store.begin_run(conn)
    for p in postings:
        store.upsert_posting(conn, p, run)
    return run


def test_same_link_under_two_company_spellings_is_one_job(conn):
    url = "https://job-boards.greenhouse.io/anduril/jobs/5236639007"
    run = _live(
        conn,
        posting("github_repo:anduril:1", company="Anduril", url=url + "?utm_source=Simplify",
                title="2027 Software Engineer Intern"),
        posting("greenhouse:anduril-industries:5236639007", company="Anduril Industries",
                url=url, title="2027 Software Engineer Intern",
                locations=("Costa Mesa, California, United States",)),
    )
    store.reconcile_duplicates(conn, run)
    survivors = [p.global_id for p in store.unalerted_postings(conn, run, "t")]
    # The copy with a description body wins, so screening and keywords have text to read.
    assert survivors == ["greenhouse:anduril-industries:5236639007"]


def test_same_title_and_city_with_different_req_ids_are_two_jobs(conn):
    run = _live(
        conn,
        posting("greenhouse:acme:1111111", url="https://boards.greenhouse.io/acme/jobs/1111111"),
        posting("greenhouse:acme:2222222", url="https://boards.greenhouse.io/acme/jobs/2222222"),
    )
    store.reconcile_duplicates(conn, run)
    assert len(store.unalerted_postings(conn, run, "t")) == 2


def test_same_title_city_spelled_differently_folds(conn):
    old = datetime.now(UTC) - timedelta(days=1)
    run = _live(
        conn,
        posting("github_repo:acme:a", locations=("Quincy, MA",), url="https://a.test/careers",
                first_seen_at=old),
        posting("lever:acme:b", locations=("Quincy, Massachusetts, United States",),
                url="https://b.test/careers"),
    )
    store.reconcile_duplicates(conn, run)
    assert [p.global_id for p in store.unalerted_postings(conn, run, "t")] == ["lever:acme:b"]


def test_a_group_already_delivered_under_another_copy_is_not_sent_again(conn):
    """A copy alerted before the sweep chose a different canonical counts for the group."""
    url = "https://jobs.ashbyhq.com/acme/1e8d195c-e28e-423d-a06a-9eeb401ed4fc"
    _live(conn, posting("github_repo:acme:a", url=url))
    store.mark_alerted(conn, ["github_repo:acme:a"], datetime.now(UTC), profile="t")
    run = _live(conn, posting("github_repo:acme:a", url=url),
                posting("ashby:acme:x", url=url, title="SWE Intern"))
    store.reconcile_duplicates(conn, run)
    assert store.unalerted_postings(conn, run, "t") == []


def test_reconcile_is_idempotent(conn):
    url = "https://jobs.ashbyhq.com/acme/1e8d195c-e28e-423d-a06a-9eeb401ed4fc"
    run = _live(conn, posting("ashby:acme:x", url=url),
                posting("github_repo:acme:a", url=url + "?ref=x", title="Other"))
    store.reconcile_duplicates(conn, run)
    assert store.reconcile_duplicates(conn, run) == 0


def test_fold_repeats_drops_a_second_entry_with_the_same_link():
    class C:
        def __init__(self, gid, url):
            self.posting = posting(gid, url=url)
            self.global_id = gid

    url = "https://jobs.ashbyhq.com/acme/1e8d195c-e28e-423d-a06a-9eeb401ed4fc"
    _, digest, recap, folded = runner._fold_repeats(
        [C("a:x:1", url)], [C("b:x:2", url + "?utm_source=x"), C("c:x:3", None)],
        [C("d:x:4", url)],
    )
    assert [c.global_id for c in digest] == ["c:x:3"] and recap == []
    assert folded == ["b:x:2"]


# --- title-first classification --------------------------------------------------------

@pytest.mark.parametrize("title,primary", [
    ("Software Engineering Intern - Summer 2027", "swe"),
    ("Technology Developer Intern", "swe"),
    ("Flight Software Intern", "systems"),
    ("AI Engineer Intern", "mle"),
    ("Electrical Engineering Co-op", "hardware"),
    ("Product Management Intern", "product"),
    ("Business Operations Internship", "business"),
    ("Data Analyst Intern", "data"),
])
def test_bodyless_titles_are_classified(title, primary):
    assert role.classify_function(title, "")[0] == primary


@pytest.mark.parametrize("title,expected", [
    ("Strategic Finance, AI & Business Partnership", ["business"]),
    ("Product Designer, AI Native", ["product"]),
    ("Personnel Security Specialist", ["business"]),
    ("Security Software Engineer", ["security", "swe"]),
])
def test_the_role_noun_outranks_a_technical_modifier(title, expected):
    """These used to be grouped at the TOP of the digest as ML / security roles."""
    assert role.classify_function(title, "") == expected


def test_collapsed_location_list_does_not_glue_its_label_to_a_city():
    from jobmon.adapters.github_repos import _split_locations
    cell = "<details><summary>4 locations</summary>Palo Alto, CA</br>Seattle, WA</details>"
    assert _split_locations(cell) == ["Palo Alto, CA", "Seattle, WA"]


def test_the_title_decides_over_body_mentions():
    """A SWE req whose body says "security" a lot is still filed under swe."""
    assert role.classify_function("Software Engineer, New Grad", "security " * 10) == ["swe"]


# --- the screener -----------------------------------------------------------------------

CONFIGURED = {"model": "test-model", "base_url": "https://luminal.invalid/v1",
              "api_key": "test-key"}


def luminal(handler, **kw) -> screen.Screener:
    """A screener whose HTTP calls go to `handler(request) -> httpx.Response`."""
    import httpx
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return screen.Screener({}, llm_http=client, **{**CONFIGURED, **kw})


def reply(content: str):
    import httpx
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_screener_sends_an_openai_style_request_and_reads_the_verdict():
    seen = {}

    def handler(request):
        import json
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return reply('```json\n{"verdict": "clearance", "evidence": "TS/SCI required.", '
                     '"category": "swe"}\n```')

    s = luminal(handler)
    s.ensure([posting("x:acme:1", body="We need you. " * 50)])
    assert seen["url"] == "https://luminal.invalid/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "test-model"
    assert s.get("x:acme:1").verdict == "clearance"
    assert s.get("x:acme:1").evidence == "TS/SCI required."


def test_screener_caches_and_respects_its_budget():
    calls = []

    def handler(request):
        calls.append(1)
        return reply('{"verdict": "none", "evidence": "", "category": "swe"}')

    s = luminal(handler, max_calls=2)
    batch = [posting(f"x:acme:{i}") for i in range(3)]
    s.ensure(batch)
    assert len(calls) == 2 and s.unscreened == 1
    s.ensure(batch[:2])
    assert len(calls) == 2, "cached verdicts must not be paid for twice"
    assert "budget" in s.note()


def test_a_wrong_key_stops_calling_for_the_rest_of_the_run():
    import httpx
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    s = luminal(handler, workers=1)
    s.ensure([posting(f"x:acme:{i}") for i in range(5)])
    assert len(calls) == 1 and s.unscreened == 5
    assert "LUMINAL_API_KEY" in s.note()


def test_an_unconfigured_screener_makes_no_calls_and_says_why(monkeypatch):
    for name in ("LUMINAL_BASE_URL", "LUMINAL_API_KEY", "LUMINAL_MODEL"):
        monkeypatch.delenv(name, raising=False)
    s = screen.Screener({})
    s.ensure([posting("x:acme:1")])
    assert s.calls == 0 and s.unscreened == 1
    assert "not configured" in s.note() and "LUMINAL_BASE_URL" in s.note()


def test_an_unusable_reply_leaves_the_posting_unscreened_not_cleared():
    s = luminal(lambda request: reply("I think this job is probably fine."))
    s.ensure([posting("x:acme:1")])
    assert s.get("x:acme:1") is None and s.unscreened == 1


@pytest.mark.parametrize("text,expected", [
    ('{"verdict": "none", "evidence": "", "category": "mle"}', ("none", "", "mle")),
    ('Sure! {"verdict": "Citizenship", "evidence": "US persons only", "category": "x"}',
     ("citizenship", "US persons only", "other")),
    ('{"verdict": "maybe"}', None),
    ("not json", None),
])
def test_parse_reply(text, expected):
    assert screen.parse_reply(text) == expected


def test_a_long_posting_is_sent_with_its_tail_intact():
    import json
    seen = {}

    def handler(request):
        seen["prompt"] = json.loads(request.content)["messages"][1]["content"]
        return reply('{"verdict": "clearance", "evidence": "", "category": "swe"}')

    body = "filler. " * 20_000 + "Requires an active TS/SCI clearance."
    luminal(handler).ensure([posting("x:acme:1", body=body)])
    assert seen["prompt"].endswith("Requires an active TS/SCI clearance.\n--- END ---")


def test_cache_round_trips(tmp_path):
    cache = {"a:b:1": screen.Screen(global_id="a:b:1", verdict="clearance", evidence="TS/SCI")}
    screen.save_cache(cache, tmp_path)
    assert screen.load_cache(tmp_path)["a:b:1"].verdict == "clearance"
    screen.save_cache(cache, tmp_path, keep=set())
    assert screen.load_cache(tmp_path) == {}


def test_env_file_fills_unset_names_only(tmp_path, monkeypatch):
    from jobmon import cli
    env = tmp_path / ".env"
    env.write_text('# c\nLUMINAL_MODEL="m-1"\nLUMINAL_API_KEY=from-file\nLUMINAL_BASE_URL=\n',
                   encoding="utf-8")
    monkeypatch.delenv("LUMINAL_MODEL", raising=False)
    monkeypatch.delenv("LUMINAL_BASE_URL", raising=False)
    monkeypatch.setenv("LUMINAL_API_KEY", "from-shell")
    assert cli.load_env_file(env) == ["LUMINAL_MODEL"]
    import os
    assert os.environ["LUMINAL_MODEL"] == "m-1"
    assert os.environ["LUMINAL_API_KEY"] == "from-shell", "a set variable wins over the file"
    assert "LUMINAL_BASE_URL" not in os.environ, "a blank value sets nothing"


def test_profile_screening_block_parses_and_rejects_typos():
    profile = parse({"screening": {"enabled": True, "on_unclear": "suppress"}}, name="p")
    assert profile.screening.enabled and profile.screening.on_unclear == "suppress"
    assert Profile().screening.enabled is False, "off by default"
    with pytest.raises(ProfileError):
        parse({"screening": {"enabeld": True}}, name="p")
