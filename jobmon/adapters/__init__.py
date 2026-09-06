"""Source adapters. Each one fetches a board and yields raw payloads (spec 5).

Rule that matters more than any interface here: **each adapter fails loudly and
independently. One broken adapter never kills the run.** The runner catches per-adapter
exceptions, records `AdapterHealth`, and continues.

Tiers, in build order:
  5a `ats_public`  - public JSON/XML boards. Cheap, stable, do this first.
  5c `github_repos`- curated cycle repos. Primary big-tech source.
  5b `bigtech/`    - hand-reverse-engineered portals. Only where the repos cover poorly.

Not scraped, on purpose: LinkedIn, Indeed, ZipRecruiter, Glassdoor.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol

import httpx


class Adapter(Protocol):
    """What the runner needs from every source module.

    Deliberately thin - spec 12 forbids abstractions for adapters not yet written. If a
    third adapter does not fit this shape, change the shape rather than adding a layer.
    """

    name: str  # goes in `Posting.source` and in the health line

    def fetch(self, client: httpx.Client) -> Iterator[dict[str, Any]]:
        """Yield raw source payloads. Normalization is `normalize.py`'s job, not the
        adapter's. Raise on transport/parse failure; the runner records the health line."""
        ...


def build_client(timeout: float = 20.0) -> httpx.Client:
    """Shared httpx client: explicit timeouts, a real User-Agent, sane redirect policy.

    TODO: set a contact User-Agent (politeness, spec 5a), `follow_redirects=True`, and
    per-host connection limits. One client for the whole run - not one per adapter.
    """
    raise NotImplementedError


def polite_get(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    """GET with rate limiting and bounded retry.

    TODO: minimum delay between requests to the same host, retry on 429/5xx with backoff
    honoring `Retry-After`, and give up after ~3 attempts. Never retry a 404.
    """
    raise NotImplementedError
