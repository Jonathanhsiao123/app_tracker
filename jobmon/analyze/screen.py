"""LLM clearance screen: a cheap model reads every posting before it is sent.

WHY THIS EXISTS. `filters/eligibility` catches clearance language with regexes, and the
regexes are narrow by design (a false `blocked` costs a job you could have taken). In
practice that meant postings reached the digest whose requirement sat in the last
paragraph - "Must be able to obtain and maintain a Secret security clearance", "eligible
for a DoD clearance", "This role requires U.S. citizenship" - phrasings the patterns miss,
discovered only after reading to the end. Curated-repo rows are worse: they carry no body
at all, so the regex has nothing to read.

So, when a profile turns `screening.enabled` on, every posting that is about to be SENT
(digest, instant or still-open recap) is read by a small model first. The model sees the
WHOLE description - when one is unusually long, the tail is the part kept - or, for a
bodyless row, the text of the apply page itself. It answers one question with a quotable
reason: does this job require a security clearance, or US citizenship / US-person status?

ROBUSTNESS, in order of what matters:

  * Off by default, per profile (`screening.enabled`), plus a global kill switch,
    `JOBMON_LLM_SCREEN=off`, for turning it off without a commit.
  * FAILS OPEN. No API key, an outage, a rate limit or a spent per-run budget leaves the
    posting unscreened and still delivered under the regex rules - with a line in the
    digest footer saying so. A monitor that goes silent because a classifier is down is
    worse than one that lets a clearance req through.
  * Each verdict is cached in `state/screened.jsonl` and committed with the ledger, so a
    posting is paid for once, not once an hour for as long as it sits in the recap.
  * The verdict carries the model's quoted evidence, shown in `jobmon list --all`, so a
    wrong call takes two seconds to overrule - the same contract as the regex filter.

The verdict is objective and shared (one row per posting); what to DO with it is the
profile's opinion, applied in `runner`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx

from ..models import Posting
from ..normalize import html_to_text

log = logging.getLogger(__name__)

# Bump when the prompt or the verdict vocabulary changes: cached rows from an older version
# are re-screened rather than trusted.
SCREEN_VERSION = 1
DEFAULT_MODEL = "claude-haiku-5-5"
SCREEN_FILE = "screened.jsonl"

VERDICTS = ("clearance", "citizenship", "none", "unclear")
BLOCKING_VERDICTS = frozenset({"clearance", "citizenship"})
CATEGORIES = (
    "swe", "mle", "systems", "security", "research", "data", "quant",
    "hardware", "product", "business", "other",
)

# Below this many characters a "description" is a stub and the apply page is fetched.
MIN_BODY_CHARS = 400
# Postings are almost always far shorter. When one is not, keep the head (what the job is)
# and a much longer tail, because the tail is where the eligibility paragraph lives.
MAX_BODY_CHARS = 60_000
HEAD_CHARS = 12_000

SYSTEM_PROMPT = """You screen job postings for an international student on an F-1 visa \
(CPT/OPT, will later need H-1B sponsorship). They cannot hold a US security clearance and \
are not a US citizen, permanent resident or "US person".

Read the ENTIRE posting - requirements are often in the last paragraph or in an equal \
opportunity / legal footer - and return:

verdict:
- "clearance": the job requires, or asks for the ability to obtain, a US government \
security clearance of any level (Confidential, Secret, Top Secret, TS/SCI, DoD, DOE Q/L, \
Public Trust, polygraph), or the title says so (e.g. "TS/SCI", "Cleared"). Count it even \
when worded as "preferred", "ability to obtain", "eligible for" or "will be sponsored for" \
a clearance: every clearance requires US citizenship.
- "citizenship": no clearance, but the job is restricted to US citizens, permanent \
residents, or "US persons" (including ITAR/EAR export-control person requirements).
- "none": you read enough of the posting and it has neither requirement. "Must be \
authorized to work in the US", a request for sponsorship status, or a mention of ITAR as a \
skill/familiarity are NOT restrictions - answer "none" for those.
- "unclear": the text is too thin to tell (e.g. only a title, or a page that did not \
load). Do not guess from the company name alone.

evidence: the exact sentence that decided it, copied verbatim, or "" for "none"/"unclear".

category: the job's function - swe (software, web, mobile, cloud, platform), mle \
(machine learning / AI), systems (embedded, firmware, robotics, OS, compilers, GPU), \
security, research, data (data science, analytics, data engineering), quant, hardware \
(electrical, mechanical, manufacturing, other non-software engineering), product \
(product/program management, design), business (finance, operations, sales, marketing, \
HR), or other."""


@dataclass(slots=True)
class Screen:
    """One posting's verdict. One JSON object per line of `state/screened.jsonl`."""

    global_id: str
    verdict: str
    evidence: str = ""
    category: str = ""
    basis: str = "description"  # description | page | title - what the model actually read
    model: str = ""
    version: int = SCREEN_VERSION
    screened_at: str = ""

    @property
    def blocks(self) -> bool:
        return self.verdict in BLOCKING_VERDICTS


# --- persistence ---------------------------------------------------------------------------

def screen_path(state_dir: Path) -> Path:
    return state_dir / SCREEN_FILE


def load_cache(state_dir: Path) -> dict[str, Screen]:
    """Every cached verdict, keyed by global_id. A missing or unreadable line is skipped:
    the cost of a lost verdict is one more API call, never a crashed run."""
    path = screen_path(state_dir)
    if not path.exists():
        return {}
    known = {f.name for f in fields(Screen)}
    out: dict[str, Screen] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            screen = Screen(**{k: v for k, v in data.items() if k in known})
        except (ValueError, TypeError):
            log.warning("screened.jsonl: skipping unreadable line")
            continue
        out[screen.global_id] = screen
    return out


def save_cache(cache: dict[str, Screen], state_dir: Path, keep: set[str] | None = None) -> None:
    """Write the cache, sorted for a stable diff. `keep` drops verdicts for postings the
    ledger no longer tracks, so the file does not grow forever."""
    path = screen_path(state_dir)
    rows = [s for gid, s in sorted(cache.items()) if keep is None or gid in keep]
    if not rows and not path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text(
        "".join(json.dumps(asdict(s), sort_keys=True) + "\n" for s in rows), encoding="utf-8"
    )
    tmp.replace(path)


# --- the screener --------------------------------------------------------------------------

def disabled_by_env() -> bool:
    """`JOBMON_LLM_SCREEN=off` (or 0/false/no) turns screening off for every profile."""
    return os.environ.get("JOBMON_LLM_SCREEN", "").strip().lower() in ("0", "off", "false", "no")


class Screener:
    """Screens postings on demand, caching every verdict.

    `ensure` fills the cache for a batch, in parallel and within `max_calls`; `get` reads
    it. A posting `get` returns None for was not screened - budget spent or API down - and
    the caller delivers it under the regex rules, never silently drops it.
    """

    def __init__(
        self,
        cache: dict[str, Screen],
        model: str = DEFAULT_MODEL,
        http: httpx.Client | None = None,
        fetch_pages: bool = True,
        max_calls: int = 300,
        workers: int = 8,
        client=None,
    ) -> None:
        self.cache = cache
        self.model = model
        self.http = http
        self.fetch_pages = fetch_pages
        self.max_calls = max_calls
        self.workers = workers
        self.calls = 0
        self.failed = 0
        self.unscreened = 0      # wanted a verdict, did not get one (budget or failure)
        self.error: str | None = None
        self._disabled = False   # a non-retryable API error: stop calling for this run
        self._client = client
        self._lock = threading.Lock()

    # -- reading ------------------------------------------------------------------------

    def get(self, global_id: str) -> Screen | None:
        screen = self.cache.get(global_id)
        if screen is None or screen.version != SCREEN_VERSION:
            return None
        return screen

    def note(self) -> str | None:
        """One footer line when screening was incomplete this run, else None."""
        if not self.unscreened:
            return None
        why = self.error or f"the per-run budget of {self.max_calls} calls was spent"
        return (f"Clearance screen: {self.unscreened} posting(s) went out unscreened "
                f"({why}); the regex eligibility filter still applied to them.")

    # -- screening ----------------------------------------------------------------------

    def ensure(self, postings: list[Posting]) -> None:
        """Screen every posting in `postings` that has no current verdict."""
        todo = [p for p in postings if self.get(p.global_id) is None]
        if not todo:
            return
        budget = max(self.max_calls - self.calls, 0)
        if self._disabled:
            budget = 0
        over, todo = todo[budget:], todo[:budget]
        self.unscreened += len(over)
        if not todo:
            return
        self.calls += len(todo)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(self._screen_one, todo))
        for posting, screen in zip(todo, results):
            if screen is None:
                self.unscreened += 1
            else:
                self.cache[posting.global_id] = screen

    def _screen_one(self, posting: Posting) -> Screen | None:
        if self._disabled:
            return None
        text, basis = self._source_text(posting)
        try:
            parsed = self._ask(posting, text, basis)
        except Exception as exc:  # noqa: BLE001 - classified in _record_failure; never fatal
            self._record_failure(exc)
            return None
        if parsed is None:
            return None
        verdict, evidence, category = parsed
        return Screen(
            global_id=posting.global_id,
            verdict=verdict,
            evidence=evidence.strip()[:500],
            category=category,
            basis=basis,
            model=self.model,
            screened_at=datetime.now(UTC).isoformat(),
        )

    def _source_text(self, posting: Posting) -> tuple[str, str]:
        body = posting.description_text or ""
        if len(body) >= MIN_BODY_CHARS:
            return body, "description"
        if self.fetch_pages and self.http is not None and posting.apply_url:
            page = fetch_page_text(self.http, posting.apply_url)
            if len(page) >= MIN_BODY_CHARS:
                return page, "page"
        return body, "title"

    def _ask(self, posting: Posting, text: str, basis: str) -> tuple[str, str, str] | None:
        import anthropic
        from pydantic import BaseModel

        class Verdict(BaseModel):
            verdict: Literal["clearance", "citizenship", "none", "unclear"]
            evidence: str
            category: Literal[
                "swe", "mle", "systems", "security", "research", "data", "quant",
                "hardware", "product", "business", "other",
            ]

        if len(text) > MAX_BODY_CHARS:
            log.info("screen: %s body is %d chars; sending head and tail",
                     posting.global_id, len(text))
            tail = MAX_BODY_CHARS - HEAD_CHARS
            text = text[:HEAD_CHARS] + "\n[... middle omitted ...]\n" + text[-tail:]
        where = ", ".join(posting.locations) or "not stated"
        body = text if text.strip() else "(no description available)"
        prompt = (
            f"Company: {posting.company}\nTitle: {posting.title}\nLocation: {where}\n"
            f"Text source: {basis}\n\n--- POSTING ---\n{body}\n--- END ---"
        )
        client = self._get_client(anthropic)
        response = client.messages.parse(
            model=self.model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
            output_format=Verdict,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            log.warning("screen: no verdict for %s (stop_reason=%s)",
                        posting.global_id, response.stop_reason)
            return None
        out = response.parsed_output
        return out.verdict, out.evidence, out.category

    def _get_client(self, anthropic):
        with self._lock:
            if self._client is None:
                self._client = anthropic.Anthropic(timeout=60.0, max_retries=3)
            return self._client

    def _record_failure(self, exc: Exception) -> None:
        """Retryable failures cost one posting; anything else stops calling for this run,
        so a bad key produces one log line instead of three hundred."""
        try:
            import anthropic
        except ImportError:
            self._disable("the `anthropic` package is not installed")
            return
        with self._lock:
            self.failed += 1
        if isinstance(exc, anthropic.RateLimitError | anthropic.APIConnectionError):
            log.warning("screen: transient failure: %s", exc)
            with self._lock:
                self.error = self.error or f"{type(exc).__name__}"
            return
        if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
            log.warning("screen: server error %s", exc.status_code)
            with self._lock:
                self.error = self.error or f"API server error {exc.status_code}"
            return
        self._disable(f"{type(exc).__name__}: {exc}")

    def _disable(self, why: str) -> None:
        with self._lock:
            if not self._disabled:
                log.error("screen: disabled for this run - %s", why)
            self._disabled = True
            self.error = why[:200]


def fetch_page_text(http: httpx.Client, url: str) -> str:
    """Best-effort text of an apply page, for a posting that came with no body.

    Many ATS pages render server-side and come back with the full description; a
    JavaScript-only page comes back nearly empty, which the caller treats as "title only"
    and the model answers `unclear` rather than guessing.
    """
    try:
        response = http.get(url, timeout=15.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("screen: could not fetch %s: %s", url, exc)
        return ""
    if "html" not in response.headers.get("content-type", "html"):
        return ""
    return html_to_text(response.text)
