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

from .analyze import sponsorship
from .models import AdapterHealth, Analysis, Posting
from .profiles import Profile

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
        html_body=format_digest_html(ordered, standalone=True),
    )


ELIGIBILITY_RANK = {"likely_ok": 0, "unknown": 1, "blocked": 2}


def digest_rank(item: tuple[Posting, Analysis]) -> tuple[int, int, str]:
    posting, analysis = item
    eligibility = ELIGIBILITY_RANK.get(analysis.eligibility, 1)
    posted = posting.posted_at or posting.first_seen_at
    return (-posted.timestamp(), eligibility, analysis.global_id)


def format_digest_html(
    items: list[tuple[Posting, Analysis]],
    heading_level: int = 2,
    standalone: bool = False,
) -> str:
    """Render categorized openings with readable text links instead of bare URLs.

    `standalone` wraps the result in its own `<html><body>` with a title, for the legacy
    `send_digest` path. `_digest_html` nests this inside a larger document and passes
    `heading_level=3`, because an `<h2>` inside an `<h2>` section reads as a sibling.
    """
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
        sections.append(
            f"<h{heading_level}>{escape(category)}</h{heading_level}>"
            f"<ul>{''.join(entries)}</ul>"
        )
    body = "".join(sections)
    if not standalone:
        return body
    return "<html><body><h1>Jobmon Opening Workflow</h1>" + body + "</body></html>"


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


def format_alert(
    posting: Posting,
    analysis: Analysis,
    profile: Profile | None = None,
    score: float | None = None,
) -> str:
    """The alert payload (spec 10, addendum 4). Every field below is required.

    The apply URL goes last and bare, so it is tappable on a phone lock screen. The
    evidence sentence is quoted verbatim - it is the whole reason the label is trustworthy,
    and the spec is explicit that the filter labels but does not decide.

    The VISA badge is one line, always present, never buried - unless the profile says
    `needs_sponsorship: false`, in which case it is omitted entirely (T8.2). A badge about
    a constraint you do not have is pure noise, and the point of this whole addendum is to
    stop producing that.
    """
    lines = [f"{posting.company} - {posting.title}"]
    where = ", ".join(posting.locations) or "location not stated"
    if posting.remote_type:
        where += f" ({posting.remote_type})"
    lines.append(where)

    if profile is None or profile.eligibility.needs_sponsorship:
        lines.append(sponsorship.badge(
            analysis.eligibility,
            analysis.sponsor_confidence,
            analysis.sponsor_evidence,
            analysis.eligibility_evidence,
        ))

    if analysis.grad_window:
        lines.append(f"Grad window: {analysis.grad_window}")

    label = analysis.eligibility
    if analysis.degree_gated:
        label += " | degree-gated"
    lines.append(f"Eligibility: {label}")
    for sentence in analysis.eligibility_evidence[:2]:
        lines.append(f'  "{sentence}"')

    if analysis.function:
        # The FULL match list, so a multi-category posting is visible as such (TX.6) even
        # though it is grouped under one category only.
        lines.append(f"Categories: {', '.join(analysis.function)}")
    if score is not None:
        lines.append(f"Score: {score:.2f}")

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


# --- addendum 5: profile-routed delivery -------------------------------------------------

def send_instant(client: httpx.Client, candidate, profile: Profile) -> bool:
    """One tier-1 / high-score posting, on its own, on the `routing.instant` channel.

    Individual rather than batched because that is the entire difference between instant
    and digest: an interruption you act on now, versus a list you read later.
    """
    analysis = candidate.analysis
    priority = 4 if analysis.eligibility == "likely_ok" else 3
    return _deliver(
        client,
        title=EMAIL_SUBJECT,
        body=format_alert(candidate.posting, analysis, profile, candidate.score),
        priority=priority,
        click_url=candidate.posting.apply_url,
        channel=profile.routing.instant,
    )


def send_profile_digest(
    client: httpx.Client,
    candidates: list,
    profile: Profile,
    truncated: int = 0,
    held_by_company: dict[str, int] | None = None,
    recap: list | None = None,
    recap_pool: int = 0,
    run_gap: str | None = None,
) -> bool:
    """One batch for one profile, already sorted and capped by `scoring`.

    TWO SECTIONS, and the split is the point (`limits.recap_days`):

      NEW THIS SWEEP   what this run found that you have never been shown. Full detail -
                       visa badge, evidence, keywords, gaps, variant - because this is the
                       part you are being notified ABOUT.
      STILL OPEN       the highest-scoring postings from the last `recap_days` that are
                       still live and not yet applied to. One line each, grouped by
                       category in `priorities.order`, because this part is read by
                       scanning, and the full block repeated every hour is unreadable.

    An hourly sweep finds one or two genuinely new postings, so a digest with only the
    first section is mostly the sentence "no new eligible openings were found". The second
    section is what makes an hourly mail worth opening.

    TRUNCATION IS STATED, NOT HIDDEN (T8.6, T8.7). A digest that quietly drops its
    twenty-sixth item is a digest you stop trusting, and the whole reason the caps are safe
    to set aggressively is that what they cut is still reachable with one command.
    """
    recap = recap or []
    if not candidates and not recap:
        body = "Sweep complete. No new eligible openings, and nothing still open matches."
        if run_gap:
            body += "\n\n" + run_gap
        return _deliver(
            client, title=EMAIL_SUBJECT, body=body, channel=profile.routing.digest
        )
    footer = _truncation_notice(profile, truncated, held_by_company)
    if run_gap:
        footer += ("\n" if footer else "\n\n---\n\n") + run_gap
    return _deliver(
        client,
        title=EMAIL_SUBJECT,
        body=_digest_text(candidates, recap, recap_pool, profile) + footer,
        priority=3,
        html_body=_digest_html(candidates, recap, recap_pool, profile, footer),
        channel=profile.routing.digest,
    )


def _heading(text: str) -> str:
    return text + "\n" + "=" * len(text)


def _group_by_category(candidates: list) -> dict[str, list]:
    """Group candidates under their Phase X category, preserving arrival order.

    `scoring.sort_candidates` has already applied the profile's `sort_by`, so insertion
    order IS the profile's order - a plain dict preserves it, and re-sorting here would
    silently override `sort_by: score`, `date` or `company`.
    """
    groups: dict[str, list] = defaultdict(list)
    for candidate in candidates:
        groups[candidate.group or "unclassified"].append(candidate)
    return groups


def _recap_title(profile: Profile, recap: list, recap_pool: int) -> str:
    days = profile.limits.recap_days
    window = "today" if days <= 1 else f"last {days} days"
    shown = f"{len(recap)} of {recap_pool}" if recap_pool > len(recap) else str(len(recap))
    return f"STILL OPEN - {window} ({shown})"


def _one_line(candidate) -> tuple[str, str]:
    """(what, where) for one compact recap row."""
    posting = candidate.posting
    where = ", ".join(posting.locations) or "location not stated"
    if posting.remote_type:
        where += f" ({posting.remote_type})"
    flags = [candidate.analysis.eligibility]
    if candidate.analysis.degree_gated:
        flags.append("degree-gated")
    return (
        f"{posting.company} - {posting.title}",
        f"{where} | {' | '.join(flags)} | score {candidate.score:.2f}",
    )


def _digest_text(candidates: list, recap: list, recap_pool: int, profile: Profile) -> str:
    blocks: list[str] = []
    if candidates:
        blocks.append(_heading(f"NEW THIS SWEEP ({len(candidates)})"))
        blocks.append("\n\n---\n\n".join(
            format_alert(c.posting, c.analysis, profile, c.score) for c in candidates
        ))
    if recap:
        blocks.append(_heading(_recap_title(profile, recap, recap_pool)))
        for category, group in _group_by_category(recap).items():
            lines = [f"{category.upper()} ({len(group)})"]
            for candidate in group:
                what, where = _one_line(candidate)
                lines.append("  " + what)
                lines.append("    " + where)
                lines.append("    " + candidate.posting.apply_url)
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _digest_html(
    candidates: list, recap: list, recap_pool: int, profile: Profile, footer: str
) -> str:
    style = "font-family:system-ui,-apple-system,sans-serif;max-width:760px"
    parts = [
        f'<html><body style="{style}">',
        "<h1>Jobmon Opening Workflow</h1>",
    ]
    if candidates:
        parts.append(f"<h2>New this sweep ({len(candidates)})</h2>")
        parts.append(format_digest_html(
            [(c.posting, c.analysis) for c in candidates], heading_level=3
        ))
    if recap:
        parts.append(f"<h2>{escape(_recap_title(profile, recap, recap_pool))}</h2>")
        for category, group in _group_by_category(recap).items():
            parts.append(f"<h3>{escape(category.title())} ({len(group)})</h3><ul>")
            for candidate in group:
                what, where = _one_line(candidate)
                url = escape(candidate.posting.apply_url, quote=True)
                parts.append(
                    f'<li><a href="{url}">{escape(what)}</a><br>'
                    f'<small style="color:#555">{escape(where)}</small></li>'
                )
            parts.append("</ul>")
    if footer:
        parts.append(
            f'<hr><p style="color:#555"><em>{escape(footer.strip())}</em></p>'
        )
    parts.append("</body></html>")
    return "".join(parts)


def _truncation_notice(
    profile: Profile, truncated: int, held_by_company: dict[str, int] | None
) -> str:
    notes: list[str] = []
    if truncated:
        notes.append(
            f"{truncated} further matching posting(s) were held back by "
            f"max_digest_items={profile.limits.max_digest_items}."
        )
    for company, count in sorted((held_by_company or {}).items()):
        notes.append(
            f"{count} more req(s) from {company} held back by "
            f"max_per_company_per_day={profile.limits.max_per_company_per_day}."
        )
    if not notes:
        return ""
    notes.append(f"See all of them with: jobmon list --profile {profile.name} --all")
    return "\n\n---\n\n" + "\n".join(notes)


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
    channel: str = "all",
) -> bool:
    """Deliver on the channel(s) a profile's `routing` block names.

    `channel` is one of ntfy | email | all | none (addendum 3). `all` is the default and
    the pre-addendum behaviour: every configured transport, and ALL of them must succeed
    before the runner records the alert, so a partial failure retries next run instead of
    silently losing one channel.

    `none` returns True. A posting routed to `silent` was never meant to be sent, so
    "delivered nothing successfully" is the honest answer - returning False would make the
    runner retry it forever.
    """
    if channel == "none":
        return True
    ntfy_ok = True
    email_ok = True
    if channel in ("ntfy", "all"):
        ntfy_ok = _post_ntfy(
            client, title=title, body=body, priority=priority, click_url=click_url
        )
    if channel in ("email", "all"):
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
