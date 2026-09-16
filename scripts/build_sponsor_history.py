"""Build `jobmon/config/sponsor_history.yaml` from DOL LCA disclosure data (addendum 4).

    python scripts/build_sponsor_history.py --input ~/dol/ --out jobmon/config/sponsor_history.yaml

The LCA disclosure files are public, quarterly, bulk-downloadable, and not scraped from
anywhere: https://www.dol.gov/agencies/eta/foreign-labor/performance

DELIBERATELY NO DOWNLOADER. The files are ~100MB .xlsx per quarter behind a page whose
URLs change shape every year, and a downloader that silently fetches last year's file is
worse than no downloader. Convert the quarters you want to CSV and point `--input` at the
directory. Any CSV with an employer-name column and, optionally, a case-status column
works; the column names below cover every layout DOL has shipped since FY2020.

WHAT IT DOES
  1. Reads every CSV under `--input`, counting CERTIFIED filings per employer name.
  2. Fuzzy-matches those employer names against `companies.yaml`, because DOL writes
     "GOOGLE LLC" and the board list says "Google".
  3. Buckets counts into `frequent` / `occasional` / `none_observed`.
  4. Writes YAML, preserving any `note:` you have hand-added to an existing entry.

WHAT IT DOES NOT DO. It does not decide anything. `none_observed` is weak evidence, not a
block; the classifier treats it as a downrank, and the alert says so.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from jobmon.normalize import normalize_company

DEFAULT_OUT = REPO_ROOT / "jobmon" / "config" / "sponsor_history.yaml"
COMPANIES = REPO_ROOT / "jobmon" / "config" / "companies.yaml"

# Every spelling DOL has used for these two columns. Matched case-insensitively after
# stripping non-alphanumerics, so "EMPLOYER_NAME", "Employer Name" and "employer.name"
# all land on the same key.
EMPLOYER_COLUMNS = ("employername", "employerbusinessdbaname", "lcacaseemployername")
STATUS_COLUMNS = ("casestatus", "status")
CERTIFIED = ("certified", "certified-withdrawn", "certifiedwithdrawn")

# Addendum 4's suggested cutoffs. "Adjust after you see the distribution" - so they are
# flags, not constants buried in a comparison.
FREQUENT_MIN = 50
OCCASIONAL_MIN = 5

# Corporate suffixes that fragment one employer across several legal entities. Stripped
# before matching; kept in the reported name.
SUFFIXES = re.compile(
    r"\b(?:inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|llp|lp|gmbh|"
    r"holdings?|group|usa|us|america|technologies|technology|labs?|systems?)\b\.?",
    re.IGNORECASE,
)


def match_key(name: str) -> str:
    """The token string two names are compared on. Suffixes removed, then normalized."""
    return normalize_company(SUFFIXES.sub(" ", name or ""))


def read_counts(directory: Path) -> Counter:
    """Certified filings per raw employer name, across every CSV under `directory`.

    A row with no recognisable status column is counted: several older DOL exports split
    certified and denied filings into separate files and carry no status column at all.
    """
    counts: Counter = Counter()
    files = sorted(directory.rglob("*.csv"))
    if not files:
        raise SystemExit(f"no .csv files under {directory} - convert the DOL .xlsx first")
    for path in files:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            reader = csv.DictReader(fh)
            headers = {_squash(h): h for h in (reader.fieldnames or [])}
            employer_col = next((headers[c] for c in EMPLOYER_COLUMNS if c in headers), None)
            if employer_col is None:
                print(f"  skipping {path.name}: no employer-name column", file=sys.stderr)
                continue
            status_col = next((headers[c] for c in STATUS_COLUMNS if c in headers), None)
            kept = 0
            for row in reader:
                if status_col is not None:
                    status = _squash(row.get(status_col) or "")
                    if status and status not in CERTIFIED:
                        continue
                name = (row.get(employer_col) or "").strip()
                if name:
                    counts[name] += 1
                    kept += 1
            print(f"  {path.name}: {kept} certified filings")
    return counts


def _squash(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def load_targets(path: Path) -> dict[str, str]:
    """match_key -> display name, for every company in companies.yaml."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for entry in raw.get("ats") or []:
        name = entry.get("company")
        if name:
            out[match_key(name)] = name
    return out


def aggregate(counts: Counter, targets: dict[str, str]) -> dict[str, int]:
    """Fold DOL employer names onto companies.yaml entries. Returns display name -> filings.

    Matching is token-prefix in both directions after suffix stripping: "GOOGLE LLC" ->
    "google" matches the target "Google" -> "google", and "STRIPE PAYMENTS COMPANY" ->
    "stripe payments" matches the target "Stripe" -> "stripe". This over-matches on short
    one-token names, which is exactly the fragmentation limitation the alert badge states
    rather than hides.
    """
    totals: dict[str, int] = {}
    by_tokens = {key: key.split() for key in targets if key}
    for employer, count in counts.items():
        tokens = match_key(employer).split()
        if not tokens:
            continue
        best: tuple[int, str] | None = None
        for key, target_tokens in by_tokens.items():
            n = len(target_tokens)
            if tokens[:n] == target_tokens and (best is None or n > best[0]):
                best = (n, key)
        if best is not None:
            display = targets[best[1]]
            totals[display] = totals.get(display, 0) + count
    return totals


def bucket(filings: int, frequent_min: int, occasional_min: int) -> str:
    if filings >= frequent_min:
        return "frequent"
    if filings >= occasional_min:
        return "occasional"
    return "none_observed"


def build(
    totals: dict[str, int],
    existing: dict,
    frequent_min: int = FREQUENT_MIN,
    occasional_min: int = OCCASIONAL_MIN,
    since: int | None = None,
    today: date | None = None,
) -> dict:
    """Assemble the YAML body, preserving hand-written `note:` fields.

    The notes are the part of this file a human wrote ("new grad SWE reqs sponsor; defense
    subsidiary does not") and the part a regenerated file would otherwise destroy.
    """
    checked = (today or datetime.now(UTC).date()).isoformat()
    since = since or (today or datetime.now(UTC).date()).year - 2
    out: dict = {}
    for company, filings in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])):
        key = company.lower().replace(" ", "_")
        prior = (existing.get(key) or {}) if isinstance(existing, dict) else {}
        block = {
            "confidence": bucket(filings, frequent_min, occasional_min),
            "h1b_filings_recent": filings,
            "source": "dol_lca_disclosure",
            "checked_on": checked,
            "since": since,
        }
        if prior.get("note"):
            block["note"] = prior["note"]
        out[key] = block
    # Hand-written entries for companies the data did not match are kept as-is: a manual
    # override is worth more than a fuzzy-match miss, and deleting it silently is how you
    # lose the one entry you researched.
    for key, block in (existing or {}).items():
        if key not in out and isinstance(block, dict):
            out[key] = block
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, required=True,
                        help="directory of DOL LCA disclosure CSVs")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--companies", type=Path, default=COMPANIES)
    parser.add_argument("--frequent-min", type=int, default=FREQUENT_MIN)
    parser.add_argument("--occasional-min", type=int, default=OCCASIONAL_MIN)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the distribution and write nothing")
    args = parser.parse_args(argv)

    print(f"reading {args.input}")
    counts = read_counts(args.input)
    targets = load_targets(args.companies)
    totals = aggregate(counts, targets)
    print(f"\n{len(counts)} distinct DOL employers -> {len(totals)} of "
          f"{len(targets)} tracked companies matched")

    tiers = Counter(
        bucket(n, args.frequent_min, args.occasional_min) for n in totals.values()
    )
    for tier in ("frequent", "occasional", "none_observed"):
        print(f"  {tier:<14} {tiers.get(tier, 0)}")
    print("\ntop matched employers:")
    for company, n in sorted(totals.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {company:<32} {n}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    existing = {}
    if args.out.exists():
        existing = yaml.safe_load(args.out.read_text(encoding="utf-8")) or {}
    body = build(totals, existing, args.frequent_min, args.occasional_min)
    header = _preserved_header(args.out)
    args.out.write_text(
        header + yaml.safe_dump(body, sort_keys=True, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"\nwrote {len(body)} entries to {args.out}")
    return 0


def _preserved_header(path: Path) -> str:
    """Keep the leading comment block of the existing file. It documents the format and
    the limitations, and regenerating the file must not throw that away."""
    if not path.exists():
        return ""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            lines.append(line)
        else:
            break
    return "\n".join(lines).rstrip() + "\n\n" if lines else ""


if __name__ == "__main__":
    raise SystemExit(main())
