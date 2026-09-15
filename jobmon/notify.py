"""Notifications (spec 10).

Transport: an ntfy.sh topic or a Telegram bot. Both are a few lines and free.
Email is optional and uses SMTP when configured. The ntfy path remains the push channel.

  Tier 1 companies (hand-listed, ~20): instant individual alert.
    Every eligible opening:              one hourly batch.
"""

from __future__ import annotations

import logging
import os
import smtplib
from collections import defaultdict
from email.message import EmailMessage
from html import escape

import httpx

from .models import AdapterHealth, Analysis, Posting

NTFY_TOPIC = os.environ.get("JOBMON_NTFY_TOPIC")
NTFY_SERVER = os.environ.get("JOBMON_NTFY_SERVER", "https://ntfy.sh")

log = logging.getLogger(__name__)

EMAIL_SUBJECT = "Jobmon Opening Workflow"

def send_alert(client: httpx.Client, posting: Posting, analysis: Analysis) -> bool:
    """Instant individual alert for a tier-1 posting. Returns delivery success.

    Returns False rather than raising so the runner leaves `alerted_at` unset and the
    posting is retried next run instead of being lost.
    """
    priority = 4 if analysis.eligibility == "likely_ok" else 3
    body = format_alert(posting, analysis)
    return _deliver(
        client,
        title=EMAIL_SUBJECT,
        body=body,
        priority=priority,
        click_url=posting.apply_url,
    )


def send_digest(
    client: httpx.Client, items: list[tuple[Posting, Analysis]], label: str
) -> bool:
    """Deliver one batch containing every eligible opening from this run.

    Openings are ordered newest-first and grouped by role/function in the email. An empty
    batch is still delivered as a heartbeat so SMTP recipients can verify the hourly run.
    """
    if not items:
        return _deliver(
            client,
            title=EMAIL_SUBJECT,
            body="Hourly scan complete. No new eligible openings were found.",
        )
    ordered = sorted(items, key=digest_rank)
    parts = [format_alert(p, a) for p, a in ordered]
    return _deliver(
        client,
        title=EMAIL_SUBJECT,
        body="\n\n---\n\n".join(parts),
        priority=3,
        html_body=format_digest_html(ordered),
    )


ELIGIBILITY_RANK = {"likely_ok": 0, "unknown": 1, "blocked": 2}


def digest_rank(item: tuple[Posting, Analysis]) -> tuple[int, int, str]:
    posting, analysis = item
    eligibility = ELIGIBILITY_RANK.get(analysis.eligibility, 1)
    posted = posting.posted_at or posting.first_seen_at
    return (-posted.timestamp(), eligibility, analysis.global_id)


def format_digest_html(items: list[tuple[Posting, Analysis]]) -> str:
    """Render categorized openings with readable text links instead of bare URLs."""
    groups: dict[str, list[tuple[Posting, Analysis]]] = defaultdict(list)
    for posting, analysis in items:
        category = ", ".join(analysis.function) or analysis.role_class
        groups[category.title()].append((posting, analysis))

    sections = []
    for category, group in groups.items():
        entries = []
        for posting, analysis in group:
            where = escape(", ".join(posting.locations) or "location not stated")
            if posting.remote_type:
                where += f" ({escape(posting.remote_type)})"
            entries.append(
                "<li><a href=\"{url}\"><strong>{title}</strong></a> &mdash; {company}"
                "<br><small>{where} | {eligibility}</small></li>".format(
                    url=escape(posting.apply_url, quote=True),
                    title=escape(posting.title),
                    company=escape(posting.company),
                    where=where,
                    eligibility=escape(analysis.eligibility),
                )
            )
        sections.append(f"<h2>{escape(category)}</h2><ul>{''.join(entries)}</ul>")
    return "<html><body><h1>Jobmon Opening Workflow</h1>" + "".join(sections) + "</body></html>"


def instant_rank(item: tuple[Posting, Analysis]) -> tuple[int, int, float, str]:
    """Legacy ranking helper retained for callers that still use individual alerts."""
    _, analysis = item
    best = max(analysis.variant_scores.values(), default=0.0)
    return (
        ELIGIBILITY_RANK.get(analysis.eligibility, 1),
        1 if analysis.degree_gated else 0,
        -best,
        analysis.global_id,
    )


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
    return _deliver(client, title="jobmon weekly", body="\n".join(lines), priority=3)


def send_pipeline_alert(client: httpx.Client, message: str) -> bool:
    """A pipeline failure, visibly distinct from a job alert (build step 6.4). Max priority
    and a different title prefix so it cannot be mistaken for a posting worth reading."""
    return _deliver(client, title="!! jobmon PIPELINE", body=message, priority=5)


def _deliver(
    client: httpx.Client,
    title: str,
    body: str,
    priority: int = 3,
    click_url: str | None = None,
    html_body: str | None = None,
) -> bool:
    """Deliver to ntfy and, when configured, SMTP email.

    Both configured channels must succeed before the runner marks postings alerted. A
    partial failure therefore retries next run instead of silently losing one channel.
    """
    ntfy_ok = _post_ntfy(
        client, title=title, body=body, priority=priority, click_url=click_url
    )
    email_ok = _send_email(title, body, html_body=html_body)
    return ntfy_ok and email_ok


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


def _email_config() -> dict[str, str] | None:
    """Return SMTP settings, or None when email is deliberately disabled."""
    values = {key: os.environ.get(key, "").strip() for key in (
        "JOBMON_SMTP_HOST", "JOBMON_EMAIL_FROM", "JOBMON_EMAIL_TO"
    )}
    configured = [bool(value) for value in values.values()]
    if not any(configured):
        return None
    if not all(configured):
        raise RuntimeError(
            "email requires JOBMON_SMTP_HOST, JOBMON_EMAIL_FROM, and JOBMON_EMAIL_TO"
        )
    values["JOBMON_SMTP_PORT"] = os.environ.get("JOBMON_SMTP_PORT", "587")
    values["JOBMON_SMTP_USER"] = os.environ.get("JOBMON_SMTP_USER", "").strip()
    values["JOBMON_SMTP_PASSWORD"] = os.environ.get("JOBMON_SMTP_PASSWORD", "")
    return values


def _send_email(subject: str, body: str, html_body: str | None = None) -> bool:
    """Send a multipart email through SMTP. Delivery failures are retryable."""
    config = _email_config()
    if config is None:
        return True
    message = EmailMessage()
    message["From"] = config["JOBMON_EMAIL_FROM"]
    message["To"] = config["JOBMON_EMAIL_TO"]
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    host = config["JOBMON_SMTP_HOST"]
    port = int(config["JOBMON_SMTP_PORT"])
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
                if config["JOBMON_SMTP_USER"]:
                    smtp.login(config["JOBMON_SMTP_USER"], config["JOBMON_SMTP_PASSWORD"])
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                if config["JOBMON_SMTP_USER"]:
                    smtp.login(config["JOBMON_SMTP_USER"], config["JOBMON_SMTP_PASSWORD"])
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        log.warning("email delivery failed: %s", exc)
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
      - raw["closed"] is truthy        -> suppressed (a github_repo row the source itself
                                          marked closed, spec 5c; still stored, same shape
                                          as `blocked`)

    `unclear` role and `unknown` eligibility ALERT. That is the whole point. Variant score
    is deliberately NOT a suppression rule: a weak recommendation is shown as weak, never
    used to drop a posting.
    """
    if analysis.eligibility == "blocked":
        return False, "blocked"
    if posting.raw.get("closed"):
        return False, "closed"
    if analysis.role_class == "experienced":
        return False, "experienced"
    return True, "tier1" if posting.company in tier else "digest"
