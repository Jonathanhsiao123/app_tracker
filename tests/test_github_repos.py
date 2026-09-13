"""Curated-repo table parsing (spec 5c, build step 7b).

Fixtures are trimmed to the shape actually observed in the three live repos configured in
`companies.yaml` on 2026-09-12 (see the module docstring in `adapters/github_repos.py`):
SimplifyJobs uses HTML `<table>`, one per job category, each with its own repeated header
row and a two-link Application cell (real apply link, then a `simplify.jobs/p/...`
companion); vanshb03 and speedyapply use real markdown pipe rows with one link each, and
disagree with each other on column names and order.
"""

from jobmon.adapters import github_repos as gr

# --- markdown pipe tables (vanshb03 / speedyapply) ----------------------------------

VANSH_TABLE = """\
| Company | Role | Location | Application/Link | Date Posted |
| --- | --- | --- | :---: | :---: |
| **Quora** | New Grad: Software Engineer | Remote | <a href="https://jobs.ashbyhq.com/quora/452afc2e?utm_source=vansh"><img alt="Apply"></a> | Aug 05 |
| **Chicago Trading Company** | New Grad: Associate Engineer | Chicago, IL</br>New York, NY | <a href="https://job-boards.greenhouse.io/ctccampusboard/jobs/1?utm_source=vansh"><img alt="Apply"></a> | Aug 01 |
"""


def test_markdown_table_maps_columns_by_header_name():
    rows = gr.parse_markdown_table(VANSH_TABLE, "https://vansh")
    assert rows[0] == {
        "company": "Quora",
        "title": "New Grad: Software Engineer",
        "locations": ["Remote"],
        "apply_url": "https://jobs.ashbyhq.com/quora/452afc2e?utm_source=vansh",
        "posted_text": "Aug 05",
    }


def test_br_tag_splits_multi_location_cell():
    """`bc.cell_text` alone would concatenate the two locations with no separator at all
    (its own `</br>` -> space replacement runs after its generic tag-stripper has already
    eaten the tag) - this is why location is split before cleaning, not after."""
    rows = gr.parse_markdown_table(VANSH_TABLE, "https://vansh")
    assert rows[1]["locations"] == ["Chicago, IL", "New York, NY"]


SPEEDYAPPLY_TABLE = """\
| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://careers.rivian.com"><strong>Rivian</strong></a> | SWE Intern - Vehicle Controls | Irvine, CA | $51/hr | <a href="https://jobs.ashbyhq.com/rivianvw.tech/f5aa59d9"><img alt="Apply"></a> | 2d |
"""


def test_reordered_and_renamed_columns_still_map_correctly():
    """speedyapply calls the role column "Position" and the link column "Posting", and
    inserts "Salary" between Location and it - a positional read would misassign every
    field after Location. Column mapping must go by header text (spec 7b)."""
    rows = gr.parse_markdown_table(SPEEDYAPPLY_TABLE, "https://speedyapply")
    assert rows[0]["title"] == "SWE Intern - Vehicle Controls"
    assert rows[0]["locations"] == ["Irvine, CA"]
    assert rows[0]["apply_url"] == "https://jobs.ashbyhq.com/rivianvw.tech/f5aa59d9"
    assert rows[0]["posted_text"] == "2d"


# --- HTML tables (SimplifyJobs) -----------------------------------------------------

SIMPLIFY_TABLE = """\
<table>
<thead>
<tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Age</th></tr>
</thead>
<tbody>
<tr>
<td><strong><a href="https://simplify.jobs/c/Veeam-Software">Veeam Software</a></strong></td>
<td>Software Engineer Intern</td>
<td>San Jose, CA</td>
<td><a href="https://job-boards.eu.greenhouse.io/veeamsoftware/jobs/1"><img alt="Apply"></a> <a href="https://simplify.jobs/p/abc"><img alt="Simplify"></a></td>
<td>0d</td>
</tr>
<tr>
<td>↳</td>
<td>Policy Engineering Intern</td>
<td>San Jose, CA</td>
<td><a href="https://job-boards.eu.greenhouse.io/veeamsoftware/jobs/2"><img alt="Apply"></a> <a href="https://simplify.jobs/p/def"><img alt="Simplify"></a></td>
<td>0d</td>
</tr>
</tbody>
</table>
"""


def test_html_table_picks_real_apply_link_over_simplify_companion_link():
    """Application cells here carry two links: the real ATS apply link, then a
    `simplify.jobs/p/...` companion link Simplify adds to every row. The companion must
    never win."""
    rows = gr.parse_markdown_table(SIMPLIFY_TABLE, "https://simplify")
    assert rows[0]["apply_url"] == "https://job-boards.eu.greenhouse.io/veeamsoftware/jobs/1"


def test_continuation_row_inherits_company():
    """A company cell of "↳" means "same company as the row above" - the convention both
    source formats use for a second role at one employer."""
    rows = gr.parse_markdown_table(SIMPLIFY_TABLE, "https://simplify")
    assert rows[1]["company"] == "Veeam Software"
    assert rows[1]["title"] == "Policy Engineering Intern"


MULTI_CATEGORY_DOC = """\
<table>
<thead><tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Age</th></tr></thead>
<tbody>
<tr><td><strong>Acme</strong></td><td>SWE Intern</td><td>NYC</td><td><a href="https://jobs.lever.co/acme/1"><img alt="Apply"></a></td><td>0d</td></tr>
</tbody>
</table>

<table>
<thead><tr><th>Company</th><th>Role</th><th>Location</th><th>Salary</th><th>Application</th><th>Age</th></tr></thead>
<tbody>
<tr><td><strong>Widgetco</strong></td><td>Data Science Intern</td><td>SF</td><td>$50/hr</td><td><a href="https://jobs.lever.co/widgetco/1"><img alt="Apply"></a></td><td>0d</td></tr>
</tbody>
</table>
"""


def test_header_remaps_at_each_sub_table_boundary():
    """Simplify has one table per job category, each with its own header row. The second
    table here inserts a Salary column the first doesn't have - if column mapping only ran
    once, off the first header, the second table's Application/Age cells would misread."""
    rows = gr.parse_markdown_table(MULTI_CATEGORY_DOC, "https://simplify")
    assert len(rows) == 2
    assert rows[1]["company"] == "Widgetco"
    assert rows[1]["title"] == "Data Science Intern"
    assert rows[1]["apply_url"] == "https://jobs.lever.co/widgetco/1"


# --- rows that are not postings ------------------------------------------------------

def test_row_with_no_link_is_dropped():
    """A banner or section-summary row carries no apply link at all and is not a posting."""
    doc = (
        "| Company | Role | Location | Application/Link | Date Posted |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| **See more closed roles** |  |  |  |  |\n"
    )
    assert gr.parse_markdown_table(doc, "https://x") == []


def test_leading_continuation_with_no_prior_company_is_dropped_not_crashed():
    doc = (
        "| Company | Role | Location | Application/Link | Date Posted |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| ↳ | Some Role | NYC | <a href=\"https://jobs.lever.co/x/1\">Apply</a> | 0d |\n"
    )
    assert gr.parse_markdown_table(doc, "https://x") == []


def test_no_header_falls_back_to_positional_order():
    """A repo with no recognizable header at all still parses, using the documented
    Company/Role/Location/Application/Date convention (spec 5c), rather than dropping
    every row."""
    doc = "| Acme | SWE Intern | NYC | <a href=\"https://jobs.lever.co/acme/1\">Apply</a> | 0d |\n"
    rows = gr.parse_markdown_table(doc, "https://x")
    assert rows == [{
        "company": "Acme",
        "title": "SWE Intern",
        "locations": ["NYC"],
        "apply_url": "https://jobs.lever.co/acme/1",
        "posted_text": "0d",
    }]


# --- is_closed_row -------------------------------------------------------------------

def test_is_closed_row_true_for_lock_emoji():
    assert gr.is_closed_row({"company": "Acme", "title": "\U0001f512 SWE Intern"}) is True


def test_is_closed_row_true_for_closed_keyword():
    assert gr.is_closed_row({"company": "Acme", "title": "SWE Intern (Closed)"}) is True


def test_is_closed_row_false_for_ordinary_row():
    assert gr.is_closed_row({"company": "Acme", "title": "SWE Intern"}) is False


def test_is_closed_row_false_for_substring_that_is_not_the_word_closed():
    """A company or role that merely contains "closed" as part of another word (there is
    no realistic example, but the word-boundary regex is what prevents one) must not
    trip this - only case-insensitive whole-word matches should."""
    assert gr.is_closed_row({"company": "Acme", "title": "Enclosed Systems Intern"}) is False


# --- fetch(): per-repo isolation -----------------------------------------------------

def test_one_repo_failing_to_fetch_does_not_abort_the_others(monkeypatch):
    """Spec 5b: one broken source never kills the sweep. `fetch` catches per-repo, not just
    per-adapter - there is only one `github_repo` adapter, so those are the same boundary
    here, same as the fix in runner.py for ats_public."""
    calls = []

    def fake_readme(client, url, day):
        if "bad" in url:
            raise RuntimeError("404")
        calls.append(url)
        return VANSH_TABLE

    monkeypatch.setattr(gr, "_fetch_readme", fake_readme)
    companies = {"github_repos": [{"url": "https://bad", "kind": "both"},
                                   {"url": "https://good", "kind": "both"}]}
    rows = list(gr.fetch(object(), companies))
    assert calls == ["https://good"]
    assert len(rows) == 2  # both VANSH_TABLE postings, from the one repo that didn't fail


def test_fetch_tags_every_row_for_the_normalizer_and_carries_closed_flag(monkeypatch):
    companies = {"github_repos": [{"url": "https://vansh", "kind": "new_grad"}]}
    monkeypatch.setattr(gr, "_fetch_readme", lambda client, url, day: VANSH_TABLE)
    rows = list(gr.fetch(object(), companies))
    assert rows[0]["_ats"] == "github_repo"
    assert rows[0]["_company"] == "Quora"
    assert rows[0]["repo_kind"] == "new_grad"
    assert rows[0]["source_repo"] == "https://vansh"
    assert rows[0]["closed"] is False
