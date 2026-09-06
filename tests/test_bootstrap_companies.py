"""Testbench T1 - slug extraction from curated repo READMEs (build step 1).

No network. Every fixture below is the shape actually observed in the live READMEs on
2026-09-06, trimmed. The gate this file guards: query strings never end up in a slug, and a
non-ATS URL is dropped loudly rather than silently.
"""

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bootstrap_companies as bc

# --- T1.1 / T1.2 / T1.4: slug extraction ------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        # T1.1 query string stripped
        ("https://boards.greenhouse.io/acme?gh_jid=123", ("greenhouse", "acme")),
        ("https://job-boards.greenhouse.io/scaleai/jobs/4730836005?utm_source=Simplify", ("greenhouse", "scaleai")),
        # T1.2 trailing job path stripped
        ("https://jobs.lever.co/acme/uuid-here", ("lever", "acme")),
        ("https://jobs.lever.co/acme/", ("lever", "acme")),
        # T1.4 embed form
        ("https://boards.greenhouse.io/embed/job_app?for=acme", ("greenhouse", "acme")),
        ("https://boards.greenhouse.io/embed/job_app?token=99&for=acme", ("greenhouse", "acme")),
        # the rest of the five
        ("https://jobs.ashbyhq.com/openai/1234-abcd", ("ashby", "openai")),
        ("https://apply.workable.com/enfos-inc/j/CA15908E0A/apply?utm_source=Simplify", ("workable", "enfos-inc")),
        ("https://jobs.smartrecruiters.com/Square/743999?oga=true", ("smartrecruiters", "square")),
    ],
)
def test_extract_ats(url, expected):
    assert bc.extract_ats(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.google.com/jobs/results/12345",  # T1.5 company careers page
        "https://simplify.jobs/p/1679d1fa-953d-4b83-b4f5-e2e3a9f4cdd0",
        "https://www.figma.com",
        "https://myworkdayjobs.com/en-US/acme/job/123",
        "https://boards.greenhouse.io/embed/job_app",  # embed with no ?for= is not a slug
        "https://jobs.recruitee.com/acme",  # out of scope: five ATSes only
        "https://acme.jobs.personio.de/xml",
    ],
)
def test_non_ats_urls_are_not_extracted(url):
    assert bc.extract_ats(url) is None


# --- slug case: folded only where the board API verifiably ignores case ------------

@pytest.mark.parametrize(
    "url,expected_slug",
    [
        # case-insensitive APIs: fold to lowercase so one board is one entry
        ("https://boards.greenhouse.io/APPIAN/jobs/1", "appian"),
        ("https://jobs.ashbyhq.com/AnyScale/1234", "anyscale"),
        ("https://jobs.smartrecruiters.com/AbbVie/743", "abbvie"),
        # case-SENSITIVE APIs: preserve exactly, folding these manufactures a 404
        ("https://jobs.lever.co/Nomagic/uuid", "Nomagic"),
        ("https://apply.workable.com/onlogic-inc/j/ABC", "onlogic-inc"),
    ],
)
def test_slug_case_folding_matches_verified_api_behaviour(url, expected_slug):
    assert bc.extract_ats(url)[1] == expected_slug


def test_lever_and_workable_are_never_folded():
    """Verified 2026-09-06: jobs.lever.co/nomagic and apply.workable.com/OnLogic-Inc both
    404 while the other casing returns 200. Folding either drops a real company."""
    assert "lever" not in bc.CASE_INSENSITIVE_ATS
    assert "workable" not in bc.CASE_INSENSITIVE_ATS
    assert bc.normalize_slug("lever", "Nomagic") == "Nomagic"
    assert bc.normalize_slug("greenhouse", "Appian") == "appian"


# --- row parsing, both formats -----------------------------------------------------

HTML_README = """
<h1>Summer 2027 Tech Internships</h1>
<table>
<tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Age</th></tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Schonfeld">Schonfeld</a></strong></td>
<td>Software Engineering Intern</td><td>NYC</td>
<td><div align="center"><a href="https://job-boards.greenhouse.io/schonfeld/jobs/8180089?utm_source=Simplify&ref=Simplify"><img src="https://i.imgur.com/fbjwDvo.png" alt="Apply"></a></div></td>
<td>1d</td>
</tr>
<tr>
<td>?</td>
<td>Technology Intern - DMFI</td><td>NYC</td>
<td><a href="https://job-boards.greenhouse.io/schonfeld/jobs/8171772?utm_source=Simplify">Apply</a></td>
<td>1d</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/ENFOS">ENFOS</a></strong></td>
<td>SWE Intern</td><td>Durham, NC</td>
<td><a href="https://apply.workable.com/enfos-inc/j/CA15908E0A/apply?utm_source=Simplify">Apply</a></td>
<td>1d</td>
</tr>
<tr>
<td><strong><a href="https://simplify.jobs/c/Google">Google</a></strong></td>
<td>SWE Intern</td><td>MTV</td>
<td><a href="https://careers.google.com/jobs/results/12345">Apply</a></td>
<td>2d</td>
</tr>
</table>
"""

MD_README = """
# New Grad Positions

| Company | Role | Location | Application | Date |
| --- | --- | --- | --- | --- |
| **Chicago Trading Company** | New Grad 2027: Associate Engineer | Chicago, IL</br>New York, NY | <a href="https://job-boards.greenhouse.io/ctccampusboard/jobs/4709991005?utm_source=vansh"><img src="https://i.imgur.com/u1KNU8z.png" alt="Apply"></a> | Aug 01 |
| ↳ | Quant Trader | Chicago, IL | <a href="https://job-boards.greenhouse.io/ctccampusboard/jobs/4709991006">Apply</a> | Aug 01 |
| <a href="https://www.figma.com"><strong>Figma</strong></a> | SWE New Grad | SF | <a href="https://boards.greenhouse.io/figma/jobs/6131089004?gh_jid=6131089004"><img src="https://i.imgur.com/JpkfjIq.png" alt="Apply"/></a> | 26d |
| [Schonfeld](https://simplify.jobs/c/Schonfeld) | New Grad SWE | NYC | [Apply](https://job-boards.greenhouse.io/schonfeld/jobs/9999) | 3d |
"""


def test_html_rows_parse_with_continuation():
    rows = list(bc.iter_rows(HTML_README, "html"))
    companies = [r.company for r in rows if r.urls]
    assert companies[:3] == ["Schonfeld", "Schonfeld", "ENFOS"]  # "?" inherits


def test_markdown_rows_parse_with_continuation():
    rows = [r for r in bc.iter_rows(MD_README, "md") if r.urls]
    assert [r.company for r in rows] == [
        "Chicago Trading Company",
        "Chicago Trading Company",  # arrow inherits
        "Figma",
        "Schonfeld",
    ]


def test_image_src_is_not_treated_as_a_link():
    """Apply-button images would otherwise dominate the dropped-rows report."""
    rows = [r for r in bc.iter_rows(MD_README, "md")]
    assert not any("imgur" in u for r in rows for u in r.urls)


# --- T1.3 / T1.5 / T1.6: harvest ---------------------------------------------------

def test_dedup_across_both_repos():
    """T1.3: Schonfeld appears in both fixtures; one entry survives."""
    h = bc.harvest({"html": HTML_README, "md": MD_README})
    schonfeld = [e for e in h.entries if e["slug"] == "schonfeld"]
    assert len(schonfeld) == 1


def test_first_source_wins_the_company_name():
    """Document order is source priority. The same board named differently by two lists
    keeps the earlier list's name, which is why companies.yaml puts SimplifyJobs first."""
    a = '| **Chicago Trading Company** | R | L | [Apply](https://jobs.lever.co/ctc/1) | 1d |'
    b = '| **CTC** | R | L | [Apply](https://jobs.lever.co/ctc/2) | 1d |'
    first = bc.harvest({"simplify": a, "other": b})
    assert [e["company"] for e in first.entries] == ["Chicago Trading Company"]
    flipped = bc.harvest({"other": b, "simplify": a})
    assert [e["company"] for e in flipped.entries] == ["CTC"]


def test_non_ats_row_is_dropped_and_counted():
    """T1.5: the Google careers-page row is skipped and shows up in the report by host."""
    h = bc.harvest({"html": HTML_README})
    assert not any(e["company"] == "Google" for e in h.entries)
    assert h.dropped_rows == 1
    assert h.dropped_hosts["careers.google.com"] == 1


def test_harvest_counts_add_up():
    h = bc.harvest({"html": HTML_README, "md": MD_README})
    assert h.rows_seen == h.rows_matched + h.dropped_rows


def test_emitted_yaml_is_loadable_and_complete():
    """T1.6: safe_load clean, and every entry carries company, ats, slug, tier."""
    h = bc.harvest({"html": HTML_README, "md": MD_README})
    loaded = yaml.safe_load(bc.to_yaml(h.entries))
    assert isinstance(loaded["ats"], list) and loaded["ats"]
    for entry in loaded["ats"]:
        assert set(entry) == {"company", "ats", "slug", "tier"}
        assert entry["company"] and entry["slug"]
        assert entry["tier"] is False
        assert entry["ats"] in {"greenhouse", "lever", "ashby", "workable", "smartrecruiters"}


def test_emitted_yaml_survives_a_comma_in_a_company_name():
    entries = [{"company": 'Acme, Inc. "The Best"', "ats": "lever", "slug": "acme", "tier": False}]
    loaded = yaml.safe_load(bc.to_yaml(entries))
    assert loaded["ats"][0]["company"] == 'Acme, Inc. "The Best"'


def test_only_the_five_scoped_ats_names_are_emitted():
    assert {ats for ats, _ in bc.ATS_PATTERNS} == {
        "greenhouse",
        "lever",
        "ashby",
        "workable",
        "smartrecruiters",
    }


def test_every_emitted_ats_has_an_endpoint_template():
    """A slug whose `ats` has no adapter would stop the run at config load."""
    from jobmon.adapters.ats_public import ENDPOINTS

    assert {ats for ats, _ in bc.ATS_PATTERNS} <= set(ENDPOINTS)


def test_hot_listing_decoration_is_stripped_from_company_names():
    """SimplifyJobs prefixes hot rows with a flame; it is not part of the employer name
    and would otherwise be printed in every alert for that company."""
    md = '| **\U0001f525 Notion** | SWE Intern | NYC | [Apply](https://jobs.ashbyhq.com/notion/1) | 1d |'
    h = bc.harvest({"s": md})
    assert [e["company"] for e in h.entries] == ["Notion"]
