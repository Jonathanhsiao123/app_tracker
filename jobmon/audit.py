"""`jobmon audit --days 7` (addendum 1).

DIAGNOSE BEFORE BUILDING. The instinct on being buried in alerts is to add filters. This
report exists so that the filter you add is the one that matters, because the fix is
usually a cap and a pruned company list rather than new code.

    alerts sent            412
      by source            greenhouse 280 | lever 74 | ashby 41 | repos 17
      by company           top 10 companies and their counts
      by function          swe 201 | data 96 | unclear 71 | mle 44
      by role_class        new_grad 88 | internship 61 | unclear 263
      by eligibility       unknown 390 | likely_ok 14 | blocked 8
    applied                14
    alert-to-apply rate    3.4%

How to read it, and what each line means you should change:

  * `unclear` dominating `role_class` means `filters/role.py` is passing everything it
    cannot classify. That was a deliberate choice, and at this volume it is the wrong one.
    Set `roles.on_unclear_role: digest`.
  * A handful of companies producing most alerts means `companies.yaml` picked up large
    employers who post hundreds of reqs. Prune the list, or set
    `limits.max_per_company_per_day`.
  * `function` full of `data` and `unclear` means the function classifier is loose, not
    that you need a new subsystem. Re-rank `priorities.order`, or add to `never`.
  * Alert-to-apply under 5% means the threshold is wrong, not that the sources are wrong.
    Raise `limits.min_score`.

Counts are over ALERTS, not over postings: what the system knows is not the problem, what
it sent you is.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import store

TOP_COMPANIES = 10

# Under this, the alerts are not the problem - the threshold is (addendum 1).
HEALTHY_APPLY_RATE = 5.0


@dataclass(slots=True)
class Audit:
    days: int
    profile: str | None
    alerts_sent: int = 0
    by_source: Counter = field(default_factory=Counter)
    by_company: Counter = field(default_factory=Counter)
    by_function: Counter = field(default_factory=Counter)
    by_role_class: Counter = field(default_factory=Counter)
    by_eligibility: Counter = field(default_factory=Counter)
    by_channel: Counter = field(default_factory=Counter)
    by_profile: Counter = field(default_factory=Counter)
    applied: int = 0
    # Alerts whose posting has no analysis row. Counted separately because they are NOT
    # evidence about the classifiers: a posting restored from the ledger has no analysis
    # until it is seen again, and folding those into `unclear` makes this report confidently
    # accuse `filters/role.py` of passing everything when the rows simply are not there.
    unanalyzed: int = 0

    @property
    def apply_rate(self) -> float:
        return round(self.applied / self.alerts_sent * 100, 1) if self.alerts_sent else 0.0


def collect(
    conn: sqlite3.Connection, days: int = 7, profile: str | None = None
) -> Audit:
    """Tally the alert rows in the window, joined to their postings and analyses."""
    since = datetime.now(UTC) - timedelta(days=days)
    report = Audit(days=days, profile=profile)

    for row in store.alerts_since(conn, since, profile):
        gid = row["global_id"]
        posting = store.get_posting(conn, gid)
        analysis = store.get_analysis(conn, gid)
        report.alerts_sent += 1
        report.by_channel[row["channel"]] += 1
        report.by_profile[row["profile"]] += 1
        if posting is not None:
            report.by_source[posting.source] += 1
            report.by_company[posting.company] += 1
        if analysis is None:
            # Still bucketed, so the columns keep summing to `alerts_sent` - the first
            # thing you check - but under a label that does not read as a classifier
            # verdict, and counted again in `unanalyzed` so `diagnose` can stay quiet.
            report.unanalyzed += 1
            report.by_function["not analyzed"] += 1
            report.by_role_class["not analyzed"] += 1
            report.by_eligibility["not analyzed"] += 1
            continue
        report.by_role_class[analysis.role_class] += 1
        report.by_eligibility[analysis.eligibility] += 1
        if analysis.function:
            for fn in analysis.function:
                report.by_function[fn] += 1
        else:
            report.by_function["unclear"] += 1

    alerted_ids = {r["global_id"] for r in store.alerts_since(conn, since, profile)}
    report.applied = len(store.applied_ids(conn) & alerted_ids)
    return report


def render(report: Audit) -> str:
    """The report, plus the reading of it. The interpretation is printed alongside the
    numbers on purpose: a count with no attached decision is a number you look at twice
    and act on never."""
    lines = [
        f"window                 last {report.days} days"
        + (f", profile {report.profile!r}" if report.profile else ", all profiles"),
        f"alerts sent            {report.alerts_sent}",
    ]
    if report.alerts_sent == 0:
        lines.append("")
        lines.append("Nothing was delivered in this window. Either the run is not sending,")
        lines.append("or your filters are already doing their job - `jobmon health` says which.")
        return "\n".join(lines)

    lines.append(f"  by profile           {_bar(report.by_profile)}")
    lines.append(f"  by channel           {_bar(report.by_channel)}")
    lines.append(f"  by source            {_bar(report.by_source)}")
    lines.append(f"  by company           {_bar(report.by_company, TOP_COMPANIES)}")
    lines.append(f"  by function          {_bar(report.by_function)}")
    lines.append(f"  by role_class        {_bar(report.by_role_class)}")
    lines.append(f"  by eligibility       {_bar(report.by_eligibility)}")
    if report.unanalyzed:
        lines.append(
            f"  not analyzed         {report.unanalyzed} "
            f"({report.unanalyzed / report.alerts_sent:.0%}) - restored from the ledger, "
            f"which stores no descriptions"
        )
    lines.append(f"applied                {report.applied}")
    lines.append(f"alert-to-apply rate    {report.apply_rate}%")

    findings = diagnose(report)
    if findings:
        lines.append("")
        lines.append("Read it this way:")
        lines.extend(f"  - {f}" for f in findings)
    return "\n".join(lines)


def diagnose(report: Audit) -> list[str]:
    """Which of the three levers - role class, company list, threshold - actually matters
    for this distribution. Run before implementing anything else (addendum 1)."""
    out: list[str] = []
    total = report.alerts_sent
    if not total:
        return out

    # Only the alerts whose posting still has an analysis row can say anything about the
    # classifiers. Judging `filters/role.py` on rows that were never analyzed is how a
    # measurement step ends up recommending the wrong lever.
    classified = total - report.unanalyzed
    if report.unanalyzed:
        out.append(
            f"{report.unanalyzed}/{total} alerts are for postings with no analysis row "
            f"(restored from the ledger, which stores no descriptions). They are excluded "
            f"from the role/function/eligibility readings below; `jobmon reanalyze` after a "
            f"sweep fills them in."
        )

    unclear = report.by_role_class.get("unclear", 0)
    if classified and unclear > classified / 2:  # `unclear` is only set on analyzed rows
        out.append(
            f"`unclear` is {unclear}/{total} of role_class: filters/role.py is passing "
            f"everything it cannot classify. Set roles.on_unclear_role: digest - this "
            f"alone is a large cut."
        )
    unknown = report.by_eligibility.get("unknown", 0)
    if classified and unknown > classified * 0.7:
        out.append(
            f"`unknown` is {unknown}/{total} of eligibility, the majority case, and it is "
            f"alerting at full priority. Set eligibility.on_unknown: digest - the single "
            f"largest noise cut available."
        )
    top = report.by_company.most_common(TOP_COMPANIES)
    top_share = sum(c for _, c in top)
    if top and top_share > total / 2:
        names = ", ".join(f"{name} ({count})" for name, count in top[:3])
        out.append(
            f"{len(top)} companies produce {top_share}/{total} of all alerts ({names}...): "
            f"companies.yaml has picked up large employers who post hundreds of reqs. "
            f"Prune them, or set limits.max_per_company_per_day."
        )
    loose = report.by_function.get("data", 0) + report.by_function.get("unclear", 0)
    if classified and loose > classified / 2:
        out.append(
            f"`data` + `unclear` is {loose}/{total} of function: the function classifier "
            f"is loose. Re-rank priorities.order, or add categories to priorities.never - "
            f"you do not need a new subsystem."
        )
    if report.apply_rate < HEALTHY_APPLY_RATE:
        out.append(
            f"alert-to-apply is {report.apply_rate}%, under {HEALTHY_APPLY_RATE}%: the "
            f"threshold is wrong, not the sources. Raise limits.min_score."
        )
    if len(out) == (1 if report.unanalyzed else 0):
        out.append(
            "No single lever dominates. The caps in `limits` are the remaining tool, and "
            "they bound volume regardless of how the thresholds are calibrated."
        )
    return out


def _bar(counter: Counter, limit: int | None = None) -> str:
    items = counter.most_common(limit)
    return " | ".join(f"{name} {count}" for name, count in items) or "-"
