"""Notifications (spec 10). PHASE 1 for the ntfy path.

Transport: an ntfy.sh topic or a Telegram bot. Both are a few lines and free.
Email is avoided on purpose - it gets buried.

  Tier 1 companies (hand-listed, ~20): instant individual alert.
  Everything else:                     batched digest, twice daily.
"""

from __future__ import annotations

import logging
import os

import httpx

from .models import AdapterHealth, Analysis, Posting

NTFY_TOPIC = os.environ.get("JOBMON_NTFY_TOPIC")
NTFY_SERVER = os.environ.get("JOBMON_NTFY_SERVER", "https://ntfy.sh")

log = logging.getLogger(__name__)

MAX_DIGEST_ITEMS = 15  # ntfy truncates long bodies; beyond this, send a count and a slice


def send_alert(client: httpx.Client, posting: Posting, analysis: Analysis) -> bool:
    """Instant individual alert for a tier-1 posting. Returns delivery success.

    Returns False rather than raising so the runner leaves `alerted_at` unset and the
    posting is retried next run instead of being lost.
    """
    priority = 4 if analysis.eligibility == "likely_ok" else 3
    return _post_ntfy(
        client,
        title=f"{posting.company}: {posting.title}",
        body=format_alert(posting, analysis),
        priority=priority,
        click_url=posting.apply_url,
    )


def send_digest(
    client: httpx.Client, items: list[tuple[Posting, Analysis]], label: str
) -> bool:
    """Batched digest for everything outside tier 1, twice daily.

    Sorted so the postings worth reading first are first: explicit sponsorship, then
    unknown, then degree-gated last. Blocked postings never reach here.
    """
    if not items:
        return True
    ordered = sorted(items, key=_digest_rank)
    shown = ordered[:MAX_DIGEST_ITEMS]
    parts = [format_alert(p, a) for p, a in shown]
    if len(ordered) > len(shown):
        parts.append(f"... and {len(ordered) - len(shown)} more. Run `jobmon list`.")
    return _post_ntfy(
        client,
        title=f"{label}: {len(ordered)} postings",
        body="\n\n---\n\n".join(parts),
        priority=3,
    )


def _digest_rank(item: tuple[Posting, Analysis]) -> tuple[int, int, str]:
    _, analysis = item
    eligibility = {"likely_ok": 0, "unknown": 1, "blocked": 2}.get(analysis.eligibility, 1)
    return (eligibility, 1 if analysis.degree_gated else 0, analysis.global_id)


def format_alert(posting: Posting, analysis: Analysis) -> str:
    """The alert payload (spec 10). Every field below is required.

    The apply URL goes last and bare, so it is tappable on a phone lock screen. The
    evidence sentence is quoted verbatim - it is the whole reason the label is trustworthy,
    and the spec is explicit that the filter labels but does not decide.
    """
    lines = [f"{posting.company} - {posting.title}"]
    where = ", ".join(posting.locations) or "location not stated"
    if posting.remote_type:
        where += f" ({posting.remote_type})"
    lines.append(where)

    if analysis.grad_window:
        lines.append(f"Grad window: {analysis.grad_window}")

    label = analysis.eligibility
    if analysis.degree_gated:
        label += " | degree-gated"
    lines.append(f"Eligibility: {label}")
    for sentence in analysis.eligibility_evidence[:2]:
        lines.append(f'  "{sentence}"')

    summary = " | ".join(", ".join(terms) for terms in analysis.keywords.values() if terms)
    if summary:
        lines.append(f"Keywords: {summary}")
    if analysis.gaps:
        lines.append(f"Gaps: {', '.join(analysis.gaps)}")

    scores = analysis.variant_scores or {}
    if scores:
        top = ", ".join(
            f"{k} {v:.2f}" for k, v in sorted(scores.items(), key=lambda kv: -kv[1])[:2]
        )
        lines.append(f"Variant: {analysis.recommended_variant} ({top})")
    else:
        lines.append(f"Variant: {analysis.recommended_variant}")

    lines.append(posting.apply_url)
    return "\n".join(lines)


def send_weekly_summary(
    client: httpx.Client, counts: dict[str, object], health: list[AdapterHealth]
) -> bool:
    """Weekly summary (spec 10): counts by company and function, adapter health, and the
    new `blocked` count - so a quietly broken pipeline is visible.

    This is the tripwire for silent zero-results: an adapter that reported ok with zero
    postings is called out explicitly rather than left to inference.
    """
    lines = [f"{k}: {v}" for k, v in counts.items()]
    silent = sorted({h.adapter for h in health if h.ok and h.postings_seen == 0})
    if silent:
        lines.append("SILENT (ok but zero postings): " + ", ".join(silent))
    lines.extend(f"{h.adapter}: {h.error}" for h in health if not h.ok)
    return _post_ntfy(client, title="jobmon weekly", body="\n".join(lines), priority=3)


def send_pipeline_alert(client: httpx.Client, message: str) -> bool:
    """A pipeline failure, visibly distinct from a job alert (build step 6.4). Max priority
    and a different title prefix so it cannot be mistaken for a posting worth reading."""
    return _post_ntfy(client, title="!! jobmon PIPELINE", body=message, priority=5)


def _post_ntfy(client: httpx.Client, title: str, body: str, priority: int = 3,
               click_url: str | None = None) -> bool:
    """POST to `{NTFY_SERVER}/{NTFY_TOPIC}`.

    Refuses to run with an unset topic rather than silently no-op'ing: a monitor that
    quietly stops notifying is worse than one that crashes.
    """
    topic = os.environ.get("JOBMON_NTFY_TOPIC", NTFY_TOPIC)
    if not topic:
        raise RuntimeError("JOBMON_NTFY_TOPIC is unset; refusing to silently drop alerts")
    headers = {"X-Title": title, "X-Priority": str(priority)}
    if click_url:
        headers["X-Click"] = click_url
    try:
        response = client.post(
            f"{NTFY_SERVER}/{topic}", content=body.encode("utf-8"), headers=headers
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("ntfy delivery failed: %s", exc)
        return False
    return True


def _post_telegram(client: httpx.Client, text: str) -> bool:
    """Alternative transport, not wired up. TODO: `sendMessage` with
    `parse_mode=MarkdownV2` - remember the escaping, job titles contain `-` and `(`
    constantly."""
    raise NotImplementedError


def should_alert(posting: Posting, analysis: Analysis, tier: set[str]) -> tuple[bool, str]:
    """Return (alert_now, reason).

    Suppression rules, and only these:
      - eligibility == "blocked"      -> suppressed (still stored, see --show-blocked)
      - already alerted               -> suppressed (the runner checks `alerted_at`)
      - duplicate / repost            -> suppressed (the runner checks the upsert outcome)
      - role_class == "experienced"   -> suppressed

    `unclear` role and `unknown` eligibility ALERT. That is the whole point. Variant score
    is deliberately NOT a suppression rule: a weak recommendation is shown as weak, never
    used to drop a posting.
    """
    if analysis.eligibility == "blocked":
        return False, "blocked"
    if analysis.role_class == "experienced":
        return False, "experienced"
    return True, "tier1" if posting.company in tier else "digest"
