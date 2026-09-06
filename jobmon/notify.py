"""Notifications (spec 10). PHASE 1 for the ntfy path.

Transport: an ntfy.sh topic or a Telegram bot. Both are a few lines and free.
Email is avoided on purpose - it gets buried.

  Tier 1 companies (hand-listed, ~20): instant individual alert.
  Everything else:                     batched digest, twice daily.
"""

from __future__ import annotations

import os
from datetime import datetime

import httpx

from .models import AdapterHealth, Analysis, Posting

NTFY_TOPIC = os.environ.get("JOBMON_NTFY_TOPIC")
NTFY_SERVER = os.environ.get("JOBMON_NTFY_SERVER", "https://ntfy.sh")


def send_alert(client: httpx.Client, posting: Posting, analysis: Analysis) -> bool:
    """Instant individual alert for a tier-1 posting. Returns delivery success.

    TODO: returning False rather than raising lets the runner leave `alerted_at` unset so
    the posting is retried next run instead of being lost.
    """
    raise NotImplementedError


def send_digest(
    client: httpx.Client, items: list[tuple[Posting, Analysis]], label: str
) -> bool:
    """Batched digest for everything outside tier 1, twice daily.

    TODO: sort by (tier, eligibility likely_ok first, degree_gated last). Cap the body -
    ntfy truncates long messages, so beyond ~15 postings send a count and the top slice.
    """
    raise NotImplementedError


def format_alert(posting: Posting, analysis: Analysis) -> str:
    """The alert payload (spec 10). Every field is required:

        company, title, location
        grad_window
        eligibility label + the evidence sentence
        keyword summary line
        gap list
        recommended variant
        apply URL

    TODO: apply URL goes last and bare, so it is tappable on a phone lock screen. The
    evidence sentence is quoted verbatim - it is the reason the label is trustworthy.
    """
    raise NotImplementedError


def send_weekly_summary(
    client: httpx.Client, counts: dict[str, object], health: list[AdapterHealth]
) -> bool:
    """Weekly summary (spec 10): counts by company and function, adapter health, and the
    new `blocked` count - so a quietly broken pipeline is visible.

    TODO: this is the tripwire for the silent-zero-results failure mode. A week where an
    adapter reported ok with zero postings every run must be loud here.
    """
    raise NotImplementedError


def _post_ntfy(client: httpx.Client, title: str, body: str, priority: int = 3,
               click_url: str | None = None) -> bool:
    """TODO: POST to `{NTFY_SERVER}/{NTFY_TOPIC}` with `X-Title`, `X-Priority`, `X-Click`.
    Refuse to run with an unset topic rather than silently no-op'ing."""
    raise NotImplementedError


def _post_telegram(client: httpx.Client, text: str) -> bool:
    """TODO: alternative transport. `sendMessage` with `parse_mode=MarkdownV2` - remember
    MarkdownV2 escaping, job titles contain `-` and `(` constantly."""
    raise NotImplementedError


def should_alert(posting: Posting, analysis: Analysis, tier: set[str]) -> tuple[bool, str]:
    """Return (alert_now, reason).

    Suppression rules, and only these:
      - eligibility == "blocked"      -> suppressed (still stored, see --show-blocked)
      - already alerted               -> suppressed
      - duplicate / repost            -> suppressed, duplicate recorded
      - role_class == "experienced"   -> suppressed

    `unclear` role and `unknown` eligibility ALERT. That is the whole point.

    TODO: `now` is a parameter, not `datetime.now()`, so the twice-daily digest window is
    testable.
    """
    raise NotImplementedError
