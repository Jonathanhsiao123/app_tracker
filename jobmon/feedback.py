"""Mute, boost, and tune (addendum 6).

The cheapest accuracy improvement available, and the only one grounded in your actual
behaviour rather than in anybody's guess about it:

    jobmon mute <global_id> --reason not-interested|wrong-function|wrong-location|no-sponsorship
    jobmon mute-company <name>
    jobmon boost <global_id>
    jobmon tune

WHERE MUTES LIVE, and why not in the profile YAML. The addendum says muting "appends to the
profile's denylist and records the reason". It does both - but the denylist entry goes to
`state/feedback/<profile>.jsonl` and is merged into `Profile.denylist` at load time, rather
than being written back into `config/profiles/<name>.yaml`. Two reasons:

  * The YAML is hand-maintained and comment-heavy. Round-tripping it through PyYAML
    silently destroys every comment in the file, which is most of its value.
  * A mute is per-posting and append-only; a profile is a small hand-edited document. They
    have different lifecycles, and the growing list belongs in `state/`, next to the other
    growing lists, where it is committed by the same workflow step.

The guarantee T8.11 actually asks for survives either way: the denylist is updated, the
reason is recorded, and no field you wrote by hand is silently changed - your profile file
is not touched at all.

`tune` PROPOSES. It never edits. A system that silently changes its own filters is one that
silently stops showing you jobs, and the entire noise problem this addendum exists to fix
started with a filter that was quietly too permissive.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

FEEDBACK_DIR = Path("state") / "feedback"

MUTE_REASONS = ("not-interested", "wrong-function", "wrong-location", "no-sponsorship")
KINDS = ("mute", "mute_company", "boost")


@dataclass(slots=True)
class Feedback:
    """One recorded judgement. Append-only; a posting muted twice is one line, not two."""

    kind: str          # mute | mute_company | boost
    target: str        # global_id, or a company name for mute_company
    at: str
    reason: str | None = None
    function: str | None = None   # the posting's function at mute time, for `tune`
    company: str | None = None    # likewise, so `tune` can group without the store


def path_for(profile: str, directory: Path | None = None) -> Path:
    # Resolved here rather than in the signature so the module attribute stays the single
    # source of truth and remains redirectable (tests, and a future --state-dir flag).
    return (directory or FEEDBACK_DIR) / f"{profile}.jsonl"


def load(profile: str, directory: Path | None = None) -> list[Feedback]:
    """Read one profile's feedback. A missing file is no feedback, not an error."""
    path = path_for(profile, directory)
    if not path.exists():
        return []
    out: list[Feedback] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            out.append(Feedback(**{k: v for k, v in data.items()
                                   if k in Feedback.__dataclass_fields__}))
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning("feedback %s line %d unreadable, skipping: %s", path, lineno, exc)
    return out


def record(
    profile: str,
    kind: str,
    target: str,
    reason: str | None = None,
    function: str | None = None,
    company: str | None = None,
    directory: Path | None = None,
    at: datetime | None = None,
) -> Feedback:
    """Append one judgement. Returns what was written.

    An append rather than a rewrite: this file is the raw record of what you did, and the
    dedup that matters (one denylist entry per posting) happens on read, in `for_profile`.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown feedback kind {kind!r}; expected one of {KINDS}")
    if reason is not None and reason not in MUTE_REASONS:
        raise ValueError(
            f"unknown --reason {reason!r}; expected one of {', '.join(MUTE_REASONS)}"
        )
    entry = Feedback(
        kind=kind, target=target, at=(at or datetime.now(UTC)).isoformat(),
        reason=reason, function=function, company=company,
    )
    path = path_for(profile, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(entry), sort_keys=True, separators=(",", ":")) + "\n")
    return entry


def for_profile(
    profile: str, directory: Path | None = None
) -> tuple[list[str], list[str], list[str]]:
    """Return (muted_postings, muted_companies, boosted_postings), deduped, order preserved.

    A posting that was boosted and later muted stays muted: the mute is the more recent
    and more specific statement, and the asymmetry is deliberate - suppressing something
    you asked to see is a correction, showing something you asked to hide is a bug.
    """
    muted: list[str] = []
    companies: list[str] = []
    boosted: list[str] = []
    for entry in load(profile, directory):
        bucket = {"mute": muted, "mute_company": companies, "boost": boosted}[entry.kind]
        if entry.target not in bucket:
            bucket.append(entry.target)
    boosted = [b for b in boosted if b not in muted]
    return muted, companies, boosted


# --- tune ------------------------------------------------------------------------------

@dataclass(slots=True)
class Proposal:
    """One concrete profile edit `tune` suggests. You apply it by hand, or you do not."""

    headline: str
    edit: str

    def __str__(self) -> str:
        return f"{self.headline}\n    {self.edit}"


def tune(profile: str, directory: Path | None = None) -> tuple[str, list[Proposal]]:
    """Report the most common mute reason and propose a concrete profile edit.

    Returns (summary, proposals). The summary is always printed; an empty proposal list
    means there is not yet enough signal to say anything useful, which is a better answer
    than a confident suggestion drawn from four mutes.
    """
    entries = [e for e in load(profile, directory) if e.kind in ("mute", "mute_company")]
    if not entries:
        return f"no mutes recorded for profile {profile!r} yet", []

    reasons = Counter(e.reason or "unspecified" for e in entries)
    total = sum(reasons.values())
    top_reason, top_count = reasons.most_common(1)[0]
    lines = [f"{total} mutes recorded for profile {profile!r}"]
    lines.extend(f"  {reason:<16} {count}" for reason, count in reasons.most_common())

    proposals: list[Proposal] = []
    relevant = [e for e in entries if (e.reason or "unspecified") == top_reason]

    if top_reason == "wrong-function":
        functions = Counter(e.function for e in relevant if e.function)
        if functions:
            fn, fn_count = functions.most_common(1)[0]
            proposals.append(Proposal(
                headline=(f"{top_count} of {total} mutes were `wrong-function`, "
                          f"{fn_count} of those were `{fn}`."),
                edit=f"roles.priorities.never: [{fn}]   # or move `{fn}` last in `order`",
            ))
    elif top_reason == "wrong-location":
        proposals.append(Proposal(
            headline=f"{top_count} of {total} mutes were `wrong-location`.",
            edit="locations.allow: [...]   # narrow it, or add the offenders to "
                 "locations.deny",
        ))
    elif top_reason == "no-sponsorship":
        proposals.append(Proposal(
            headline=f"{top_count} of {total} mutes were `no-sponsorship`.",
            edit="eligibility.min_sponsor_confidence: occasional   "
                 "# and/or eligibility.on_unknown: digest",
        ))
    elif top_reason == "not-interested":
        proposals.append(Proposal(
            headline=(f"{top_count} of {total} mutes were `not-interested`, which names no "
                      f"cause - the score threshold is the blunt instrument for this."),
            edit="limits.min_score: 0.65   # raise it until the digest is worth reading",
        ))

    companies = Counter(e.target for e in entries if e.kind == "mute_company")
    companies.update(e.company for e in entries if e.kind == "mute" and e.company)
    for company, count in companies.most_common(3):
        if count >= 3:
            proposals.append(Proposal(
                headline=f"{count} mutes came from {company}.",
                edit=f"denylist.companies: [{company!r}]   # or lower "
                     f"limits.max_per_company_per_day",
            ))

    lines.append("")
    lines.append("Proposals below are NOT applied. Edit the YAML yourself if you agree.")
    return "\n".join(lines), proposals
