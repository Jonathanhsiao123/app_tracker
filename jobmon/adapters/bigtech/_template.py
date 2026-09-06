"""TEMPLATE for a big tech portal adapter. Copy, rename, fill in, add to `ENABLED`.

Endpoint:    <URL>
Method:      <GET|POST>
Params:      <query/filter params, and which ones matter>
Pagination:  <offset/page/cursor scheme, and the page size cap>
Headers:     <the minimum set that works - strip cookies and tracking headers>
Discovered:  <YYYY-MM-DD>          <- required; this is how you date the rot
Notes:       <auth? rate limit? does it 403 without a Referer?>
"""

from __future__ import annotations

from typing import Any, Iterator

import httpx

name = "TEMPLATE"  # becomes Posting.source; use the company slug, e.g. "google"


def fetch(client: httpx.Client) -> Iterator[dict[str, Any]]:
    """Page through the portal's JSON endpoint and yield raw postings.

    TODO:
      - Filter server-side where the portal supports it (early-career / intern job
        families, US locations). Cheaper than pulling every req and filtering locally.
      - Stop at the last page, and also at a hard page cap so a pagination bug cannot
        spin forever.
      - Raise on a schema surprise. Returning zero results quietly is the failure this
        whole tier is prone to.
    """
    raise NotImplementedError
