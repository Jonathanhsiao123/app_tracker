"""`scripts/build_sponsor_history.py` (addendum 4).

No download is tested, because there is no downloader: the DOL files are ~100MB .xlsx
behind a page whose URLs change shape every year, and a downloader that silently fetches
last year's file is worse than none. What IS tested is everything between a CSV on disk and
the YAML: aggregation, fuzzy employer matching, bucketing, and the note preservation that
makes regenerating the file safe.
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "build_sponsor_history", REPO_ROOT / "scripts" / "build_sponsor_history.py"
)
build_sponsor_history = importlib.util.module_from_spec(_spec)
sys.modules["build_sponsor_history"] = build_sponsor_history
_spec.loader.exec_module(build_sponsor_history)
bsh = build_sponsor_history


def write_csv(directory, name, rows, header="EMPLOYER_NAME,CASE_STATUS"):
    directory.mkdir(parents=True, exist_ok=True)
    body = "\n".join([header, *rows])
    (directory / name).write_text(body + "\n", encoding="utf-8")


# --- reading ---------------------------------------------------------------------------

def test_only_certified_filings_are_counted(tmp_path):
    write_csv(tmp_path, "q1.csv", [
        "GOOGLE LLC,Certified",
        "GOOGLE LLC,Certified",
        "GOOGLE LLC,Denied",
        "GOOGLE LLC,Certified - Withdrawn",
    ])
    assert bsh.read_counts(tmp_path)["GOOGLE LLC"] == 3


def test_every_dol_column_spelling_is_recognised(tmp_path):
    write_csv(tmp_path, "a.csv", ["ACME INC,Certified"], header="Employer Name,Case Status")
    write_csv(tmp_path, "b.csv", ["ACME INC,Certified"],
              header="EMPLOYER_BUSINESS_DBA_NAME,CASE_STATUS")
    counts = bsh.read_counts(tmp_path)
    assert counts["ACME INC"] == 2


def test_a_file_with_no_status_column_counts_every_row(tmp_path):
    """Several older DOL exports split certified and denied into separate files and carry
    no status column at all."""
    write_csv(tmp_path, "old.csv", ["ACME INC", "ACME INC"], header="EMPLOYER_NAME")
    assert bsh.read_counts(tmp_path)["ACME INC"] == 2


def test_a_file_with_no_employer_column_is_skipped_not_fatal(tmp_path, capsys):
    write_csv(tmp_path, "junk.csv", ["1,2"], header="FOO,BAR")
    write_csv(tmp_path, "good.csv", ["ACME INC,Certified"])
    counts = bsh.read_counts(tmp_path)
    assert counts["ACME INC"] == 1
    assert "no employer-name column" in capsys.readouterr().err


def test_an_empty_directory_stops_with_an_explanation(tmp_path):
    with pytest.raises(SystemExit, match="convert the DOL .xlsx first"):
        bsh.read_counts(tmp_path)


# --- matching ------------------------------------------------------------------------------

def test_corporate_suffixes_are_stripped_before_matching():
    """DOL writes "GOOGLE LLC"; the board list says "Google"."""
    assert bsh.match_key("GOOGLE LLC") == bsh.match_key("Google")
    assert bsh.match_key("Stripe, Inc.") == bsh.match_key("Stripe")
    assert bsh.match_key("ACME TECHNOLOGIES INC") == bsh.match_key("Acme")


def test_aggregate_folds_several_legal_entities_onto_one_company():
    """`Name matching is fuzzy and subsidiaries fragment.` Folding them together is the
    point; the alert badge states the limitation rather than hiding it."""
    counts = {"STRIPE INC": 40, "STRIPE PAYMENTS COMPANY": 12, "UNRELATED CORP": 900}
    targets = {bsh.match_key("Stripe"): "Stripe"}
    assert bsh.aggregate(counts, targets) == {"Stripe": 52}


def test_aggregate_prefers_the_longest_matching_target():
    counts = {"ACME DEFENSE SYSTEMS INC": 3, "ACME INC": 100}
    targets = {
        bsh.match_key("Acme"): "Acme",
        bsh.match_key("Acme Defense"): "Acme Defense",
    }
    assert bsh.aggregate(counts, targets) == {"Acme": 100, "Acme Defense": 3}


def test_an_employer_matching_nothing_is_dropped():
    assert bsh.aggregate({"NOBODY LLC": 500}, {bsh.match_key("Acme"): "Acme"}) == {}


# --- bucketing ---------------------------------------------------------------------------------

def test_the_suggested_cutoffs():
    """50+ frequent, 5-49 occasional, 1-4 none_observed. Flags, not buried constants:
    `adjust after you see the distribution`."""
    assert bsh.bucket(240, 50, 5) == "frequent"
    assert bsh.bucket(50, 50, 5) == "frequent"
    assert bsh.bucket(49, 50, 5) == "occasional"
    assert bsh.bucket(5, 50, 5) == "occasional"
    assert bsh.bucket(4, 50, 5) == "none_observed"
    assert bsh.bucket(0, 50, 5) == "none_observed"
    assert bsh.bucket(30, 20, 3) == "frequent", "the cutoffs are parameters"


# --- writing -----------------------------------------------------------------------------------

def test_build_produces_entries_the_loader_accepts():
    from jobmon import config_loader

    body = bsh.build({"Acme Corp": 240, "MidCo": 12}, {}, today=date(2026, 9, 15))
    assert body["acme_corp"]["confidence"] == "frequent"
    assert body["acme_corp"]["h1b_filings_recent"] == 240
    assert body["acme_corp"]["checked_on"] == "2026-09-15"
    assert body["acme_corp"]["since"] == 2024
    assert body["midco"]["confidence"] == "occasional"

    # round-trips through the real loader without raising
    path = Path(__import__("tempfile").mkdtemp()) / "sponsor_history.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    history = config_loader.load_sponsor_history(path)
    assert history["acme"]["confidence"] == "frequent"


def test_hand_written_notes_survive_a_regeneration():
    """The notes are the part a human wrote, and the part a regenerated file would
    otherwise destroy."""
    existing = {"acme_corp": {"confidence": "frequent", "h1b_filings_recent": 1,
                              "note": "defense subsidiary does not sponsor"}}
    body = bsh.build({"Acme Corp": 240}, existing, today=date(2026, 9, 15))
    assert body["acme_corp"]["note"] == "defense subsidiary does not sponsor"
    assert body["acme_corp"]["h1b_filings_recent"] == 240, "the count is still refreshed"


def test_a_hand_added_entry_the_data_did_not_match_is_kept():
    """A manual override is worth more than a fuzzy-match miss, and deleting it silently
    is how you lose the one entry you researched."""
    existing = {"researched_co": {"confidence": "frequent", "note": "confirmed by email"}}
    body = bsh.build({"Acme Corp": 240}, existing, today=date(2026, 9, 15))
    assert body["researched_co"]["note"] == "confirmed by email"


def test_the_documentation_header_survives_a_regeneration(tmp_path):
    path = tmp_path / "sponsor_history.yaml"
    path.write_text("# how to build this\n# and what it cannot tell you\n\nacme:\n"
                    "  confidence: frequent\n", encoding="utf-8")
    header = bsh._preserved_header(path)
    assert "how to build this" in header
    assert "what it cannot tell you" in header
    assert "acme:" not in header


def test_end_to_end_against_a_synthetic_dol_export(tmp_path, capsys):
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "ats:\n"
        "  - {company: 'Acme', ats: greenhouse, slug: acme, tier: false}\n"
        "  - {company: 'MidCo', ats: lever, slug: midco, tier: false}\n",
        encoding="utf-8",
    )
    csvs = tmp_path / "dol"
    write_csv(csvs, "fy2025.csv",
              ["ACME INC,Certified"] * 60 + ["MIDCO LLC,Certified"] * 7
              + ["NOBODY CORP,Certified"] * 999)
    out = tmp_path / "sponsor_history.yaml"

    assert bsh.main(["--input", str(csvs), "--companies", str(companies),
                     "--out", str(out)]) == 0

    body = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert body["acme"]["confidence"] == "frequent"
    assert body["acme"]["h1b_filings_recent"] == 60
    assert body["midco"]["confidence"] == "occasional"
    assert "nobody" not in body
    printed = capsys.readouterr().out
    assert "2 of 2 tracked companies matched" in printed
    assert "wrote 2 entries" in printed


def test_dry_run_writes_nothing(tmp_path, capsys):
    companies = tmp_path / "companies.yaml"
    companies.write_text("ats:\n  - {company: 'Acme', ats: greenhouse, slug: a, tier: false}\n",
                         encoding="utf-8")
    csvs = tmp_path / "dol"
    write_csv(csvs, "q.csv", ["ACME INC,Certified"])
    out = tmp_path / "sponsor_history.yaml"
    bsh.main(["--input", str(csvs), "--companies", str(companies), "--out", str(out),
              "--dry-run"])
    assert not out.exists()
    assert "nothing written" in capsys.readouterr().out
