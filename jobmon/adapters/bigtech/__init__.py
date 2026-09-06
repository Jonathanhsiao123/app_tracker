"""Hand-reverse-engineered big tech portals (spec 5b). PHASE 3, and only where needed.

Google, Amazon, Microsoft, Apple, Meta and Netflix do not use the public-JSON ATSes. They
run their own portals or Workday-family systems.

**Honest note from the spec:** the curated GitHub repos already monitor these companies and
are updated within hours by a large community. Modules here duplicate free work. Build one
only for a company `github_repos` covers poorly, and treat 5c as the primary big tech source.

Discovery procedure, ~20 min per company, done once:
  1. Open the careers search page.
  2. DevTools -> Network -> filter XHR/Fetch.
  3. Page through the job list; find the request returning JSON.
  4. Copy as cURL, strip to the minimum required headers, reproduce here.
  5. Record endpoint, params and pagination scheme in the module docstring WITH THE DATE
     DISCOVERED. These endpoints change without notice.

Every module: fails loudly, fails independently, logs a health line even on zero results.
"""

from __future__ import annotations

# TODO: register modules here as they are written, so the runner can iterate them.
# Keep it a plain list. No plugin discovery, no entry points (spec 12).
ENABLED: list[str] = []
