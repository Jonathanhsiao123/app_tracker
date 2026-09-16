"""Employer sponsorship history, and how it combines with the text classifier (addendum 4).

Posting text is a weak signal for visa support. Companies that sponsor routinely usually
say nothing, and boilerplate refusal language sometimes sits on reqs a recruiter will
override. Company filing history is much stronger, and it is public: DOL LCA disclosure
files, quarterly, bulk download, no scraping. `scripts/build_sponsor_history.py` turns them
into `config/sponsor_history.yaml`; this module reads it.

COMBINE, DO NOT REPLACE. The one thing the text classifier is reliably good at is spotting
an explicit refusal, and no amount of filing history should overrule a posting that says
"Must be a U.S. citizen" (T8.5). So history only ever moves `unknown`:

    text        history           result
    blocked     any               blocked      explicit refusal wins
    likely_ok   any               likely_ok
    unknown     frequent          likely_ok    promoted, evidence names the filing count
    unknown     occasional        unknown      confidence noted
    unknown     none_observed     unknown      flagged low confidence

This is analysis, not policy: it is the same for every user, computed once, stored once.
What a profile then DOES with `likely_ok` is `profiles.Eligibility`'s business.

LIMITATIONS, stated in the alert rather than hidden (addendum 4):
  * LCA filings are H-1B petitions. They are not a promise about this req, and they say
    nothing about cap-exempt paths.
  * Name matching is fuzzy and subsidiaries fragment; one employer may appear under
    several legal entities, so a low count can be an artefact of the match, not the truth.
  * `none_observed` is weak evidence, not a block. Small companies that would sponsor
    simply have not needed to yet.
  * Clearance and ITAR are the only true hard blocks in this system. Everything else here
    is a prior.
"""

from __future__ import annotations

from typing import Any

from ..models import Eligibility, SponsorConfidence
from ..normalize import normalize_company

# Below this many recent filings, `frequent` is not claimed no matter what the file says.
# Belt and braces against a hand-edited history entry; the bucketing itself is the
# builder script's job.
PROMOTION_MIN_FILINGS = 1


def lookup(company: str, history: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Find one employer in the history file. Returns the raw block, or None.

    Token-prefix match on the normalized name, the same rule
    `filters/eligibility.apply_override` uses: an entry for "acme" has to match the posting
    company "Acme Corporation, Inc." Token-wise rather than string-wise so that a "meta"
    entry does not swallow "metabolic labs".
    """
    if not history:
        return None
    wanted = normalize_company(company).split()
    if not wanted:
        return None
    best: tuple[int, dict[str, Any]] | None = None
    for key, block in history.items():
        key_tokens = key.split()
        if not key_tokens or wanted[: len(key_tokens)] != key_tokens:
            continue
        # Longest matching prefix wins, so "acme defense" beats a bare "acme" for
        # "Acme Defense Systems" - subsidiaries are exactly where the two differ.
        if best is None or len(key_tokens) > best[0]:
            best = (len(key_tokens), block)
    return best[1] if best else None


def confidence_for(company: str, history: dict[str, dict[str, Any]]) -> SponsorConfidence:
    block = lookup(company, history)
    return str((block or {}).get("confidence", "unknown"))  # type: ignore[return-value]


def combine(
    text_label: Eligibility,
    company: str,
    history: dict[str, dict[str, Any]],
) -> tuple[Eligibility, SponsorConfidence, str | None]:
    """Fold employer history into the text label. Returns (label, confidence, evidence).

    `evidence` is the human-readable second half of the VISA badge, or None when there is
    nothing to say. It is written here, next to the table it implements, rather than in the
    notifier: the sentence and the decision have to stay in step, and a promotion whose
    evidence line does not name the filing count is not auditable.
    """
    block = lookup(company, history) or {}
    confidence: SponsorConfidence = str(block.get("confidence", "unknown"))  # type: ignore[assignment]
    filings = block.get("h1b_filings_recent")
    since = block.get("since") or block.get("checked_on")

    if text_label == "blocked":
        # T8.5. History never overrides an explicit refusal, and the badge does not muddy
        # the point by reciting a filing count next to a citizenship requirement.
        return "blocked", confidence, None

    if text_label == "likely_ok":
        return "likely_ok", confidence, _filings_phrase(filings, since)

    # text_label == "unknown" from here down.
    if confidence == "frequent" and (filings is None or filings >= PROMOTION_MIN_FILINGS):
        phrase = _filings_phrase(filings, since) or "employer sponsors frequently"
        return "likely_ok", confidence, f"no posting language; employer {phrase}"
    if confidence == "occasional":
        phrase = _filings_phrase(filings, since) or "some filing history"
        return "unknown", confidence, f"occasional sponsor - {phrase}"
    if confidence == "none_observed":
        return (
            "unknown",
            confidence,
            "employer has no observed H-1B history (weak evidence, not a block)",
        )
    return "unknown", confidence, None


def _filings_phrase(filings: Any, since: Any) -> str | None:
    if not filings:
        return None
    window = f" since {str(since)[:4]}" if since else ""
    plural = "" if filings == 1 else "s"
    return f"filed {filings} H-1B petition{plural}{window}"


def badge(
    eligibility: Eligibility,
    confidence: SponsorConfidence,
    evidence: str | None,
    blocking_evidence: list[str] | None = None,
) -> str:
    """The one-line VISA badge (addendum 4). Always present, never buried.

        VISA: likely_ok - 240 H-1B filings since 2024 - no sponsorship language in posting
        VISA: blocked   - "Must be a U.S. citizen."
        VISA: unknown   - employer has no observed H-1B history

    The caller omits the badge entirely for a profile with `needs_sponsorship: false`
    (T8.2); this function does not know about profiles and always renders one.
    """
    detail = evidence
    if eligibility == "blocked":
        quoted = (blocking_evidence or [None])[0]
        detail = f'"{quoted}"' if quoted else "blocking language in posting"
    elif detail is None:
        detail = {
            "frequent": "employer sponsors frequently",
            "occasional": "employer sponsors occasionally",
            "none_observed": "employer has no observed H-1B history",
        }.get(confidence, "no sponsorship language in posting, no employer filing history")
    return f"VISA: {eligibility:<9} - {detail}"
