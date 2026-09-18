"""Per-profile scoring, routing, caps and sort order (addendum 5, Phase X).

This is the `[profile A] -> alerts` box. It reads posting records and analysis records and
never writes either: everything here is an opinion about facts computed upstream, which is
what makes `--resort` free and a second user cost nothing.

WHY A SCORE AND NOT A FILTER. A boolean pass/fail has exactly one knob - what passes - and
the only way to get less mail out of it is to pass fewer things, which is also the only way
to start missing postings. A rank has two: what passes, and what gets read first. The caps
in `limits` then bound the volume regardless of how badly the thresholds are calibrated,
which is the actual guarantee worth having.

ROUTING IS A `min` OF TWO THINGS. The policy layer (eligibility, role class, denylists)
produces a CEILING: the loudest channel a posting is allowed to reach. The score produces
a PROPOSAL: the channel it deserves. The quieter of the two wins. That ordering is what
makes `on_unknown: digest` mean what it says - an `unknown` posting scoring 0.95 goes to
the digest, not instant - while leaving `alert` as permission rather than compulsion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .config_loader import rank_weights
from .models import ROUTE_ORDER, Analysis, Posting, Route
from .normalize import normalize_company
from .profiles import Profile, confidence_meets, confidence_score


@dataclass(slots=True)
class Candidate:
    """One posting, judged for one profile. What the notifier and `jobmon list` render."""

    posting: Posting
    analysis: Analysis
    score: float
    route: Route
    group: str                                   # Phase X grouping category
    reasons: list[str] = field(default_factory=list)   # why it landed where it did
    breakdown: dict[str, float] = field(default_factory=dict)  # per-term contributions

    @property
    def global_id(self) -> str:
        return self.posting.global_id


# score >= this, or a tier-1 company, proposes an instant alert (addendum 5).
INSTANT_THRESHOLD = 0.8


def _quieter(a: Route, b: Route) -> Route:
    return a if ROUTE_ORDER[a] <= ROUTE_ORDER[b] else b


def _disposition_ceiling(disposition: str) -> Route:
    """`suppress | digest | alert` as a ceiling. `alert` is permission, not compulsion:
    it lifts the ceiling to instant and leaves the score to decide."""
    return {"suppress": "silent", "digest": "digest", "alert": "instant"}[disposition]


# --- individual score terms -------------------------------------------------------------

def location_match(posting: Posting, profile: Profile) -> float:
    """prefer = 1.0, allow = 0.5, neither = 0.0 (addendum 5).

    An empty `allow` list means everywhere is allowed - a profile that has not thought
    about location yet must not silently score every posting at zero.
    """
    locs = profile.locations
    if _location_hits(posting, locs.prefer):
        return 1.0
    if not locs.allow or _location_hits(posting, locs.allow):
        return 0.5
    return 0.0


def _location_hits(posting: Posting, rules: list[str]) -> bool:
    if not rules:
        return False
    haystack = [loc.lower() for loc in posting.locations]
    if posting.remote_type:
        haystack.append(posting.remote_type.lower())
    for rule in rules:
        needle = rule.lower().strip()
        if not needle:
            continue
        # Containment in both directions: the rule "Bay Area" has to match the location
        # "Bay Area, CA", and the rule "United States" has to match a bare "United
        # States" as well as "Remote - United States".
        if any(needle in loc or loc in needle for loc in haystack):
            return True
    return False


def _denied_location(posting: Posting, profile: Profile) -> bool:
    return _location_hits(posting, profile.locations.deny)


def _title_denied(posting: Posting, profile: Profile) -> str | None:
    """A `denylist.title_terms` hit, matched on word boundaries.

    Boundaries matter here for the same reason they do in `filters/role`: a substring match
    on "sap" also rejects "SAP-free architecture" and, more to the point, "sapling".
    """
    title = posting.title or ""
    for term in profile.denylist.title_terms:
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", title, re.IGNORECASE):
            return term
    return None


def _company_denied(posting: Posting, profile: Profile) -> str | None:
    wanted = normalize_company(posting.company).split()
    for entry in profile.denylist.companies:
        tokens = normalize_company(entry).split()
        if tokens and wanted[: len(tokens)] == tokens:
            return entry
    return None


def score(
    posting: Posting,
    analysis: Analysis,
    profile: Profile,
    tier: set[str] | None = None,
) -> tuple[float, dict[str, float]]:
    """Addendum 5's formula. Returns (score, per-term breakdown).

        score = w_function    * function_match      (Phase X rank-derived weight)
              + w_keywords    * variant_top_score   (existing variants.py output)
              + w_location    * location_match      (allow=0.5, prefer=1.0)
              + w_eligibility * sponsor_confidence  (frequent=1.0 ... none_observed=0.3)
              + w_tier        * company_tier
              - penalty_degree_gated
              - penalty_yoe_over

    Clamped to [0, 1] at the end so that `min_score` and the 0.8 instant threshold stay
    comparable across profiles that have edited their weights. The breakdown is returned
    rather than logged because "why did this score 0.42" is the question you actually ask,
    and recomputing it by hand from eight numbers is how you stop asking.
    """
    w = profile.scoring
    priorities = profile.roles.priorities
    breakdown: dict[str, float] = {}

    breakdown["function"] = w.w_function * priorities.weight_for(list(analysis.function))
    top_variant = max(analysis.variant_scores.values(), default=0.0)
    breakdown["keywords"] = w.w_keywords * min(max(top_variant, 0.0), 1.0)
    breakdown["location"] = w.w_location * location_match(posting, profile)

    # A profile that does not need sponsorship gets a neutral, full-value term rather than
    # a zero: the whole eligibility block no-ops (T8.2), and zeroing it would quietly
    # depress every score for that user by w_eligibility.
    if profile.eligibility.needs_sponsorship:
        breakdown["eligibility"] = w.w_eligibility * confidence_score(analysis.sponsor_confidence)
    else:
        breakdown["eligibility"] = w.w_eligibility

    breakdown["tier"] = w.w_tier * (1.0 if posting.company in (tier or set()) else 0.0)

    # Deprioritized, never suppressed by the score itself: an MS/PhD floor is a strong
    # signal and a frequently wrong one, so it costs rank and leaves the decision to
    # `roles.degree_gated` in the policy layer.
    if analysis.degree_gated:
        breakdown["degree_gated"] = -w.penalty_degree_gated
    if analysis.yoe_floor is not None and analysis.yoe_floor > profile.roles.max_yoe:
        breakdown["yoe_over"] = -w.penalty_yoe_over
    if (
        profile.eligibility.needs_sponsorship
        and analysis.sponsor_confidence != "unknown"
        and not confidence_meets(
            analysis.sponsor_confidence, profile.eligibility.min_sponsor_confidence
        )
    ):
        # Addendum 4: `min_sponsor_confidence` downranks. It does not suppress unless
        # `on_below_min_sponsor` is set to say so, which the policy layer handles.
        #
        # `unknown` IS EXEMPT, and that exemption is load-bearing. `unknown` means "no DOL
        # match", which the addendum itself reads as a fuzzy-name miss or a small employer
        # - not evidence of refusal - which is why CONFIDENCE_SCORE already puts it at 0.5,
        # mid-scale, rather than at the bottom. Charging the penalty on top of that scored
        # it twice for the same missing fact.
        #
        # The cost of getting this wrong was total, not marginal. With `sponsor_history`
        # unpopulated every employer is `unknown`, so the penalty applied to 321 of 321
        # candidates on a measured sweep - a uniform offset, which ranks nothing and is
        # arithmetically just `min_score + 0.10` with the ranking signal destroyed. Between
        # that and the halved eligibility term, every posting lost 0.175 of a 1.0 scale and
        # `min_score: 0.55` admitted 0 of 321. A penalty that fires on everything is not a
        # penalty; it is a calibration error wearing one's clothes.
        breakdown["below_min_sponsor"] = -w.penalty_below_min_sponsor
    if posting.global_id in profile.boosted:
        breakdown["boost"] = w.boost_bonus

    total = sum(breakdown.values())
    return min(max(total, 0.0), 1.0), breakdown


# --- policy ceiling ----------------------------------------------------------------------

def policy_ceiling(
    posting: Posting,
    analysis: Analysis,
    profile: Profile,
    applied: set[str] | None = None,
) -> tuple[Route, list[str]]:
    """The loudest channel this posting is ALLOWED to reach, and why.

    Every rule here can only lower the ceiling, never raise it, so the order they are
    evaluated in does not change the answer - only which reasons get recorded. All of them
    run, deliberately: "suppressed: quant, denylisted company" is a more useful line in
    `jobmon list --all` than the first rule that happened to fire.
    """
    ceiling: Route = "instant"
    reasons: list[str] = []

    def lower(to: Route, why: str) -> None:
        nonlocal ceiling
        ceiling = _quieter(ceiling, to)
        reasons.append(why)

    # --- Phase X: `never` is absolute, at any score (T8.10, TX.2) ----------------------
    never = set(profile.roles.priorities.never)
    hit = never.intersection(analysis.function)
    if hit:
        lower("silent", f"category {', '.join(sorted(hit))} is in priorities.never")

    # --- denylists ---------------------------------------------------------------------
    if posting.global_id in profile.denylist.postings:
        lower("silent", "muted posting")
    company = _company_denied(posting, profile)
    if company:
        lower("silent", f"denylisted company {company!r}")
    term = _title_denied(posting, profile)
    if term:
        lower("silent", f"denylisted title term {term!r}")

    # --- location ----------------------------------------------------------------------
    if _denied_location(posting, profile):
        lower("silent", "location is on the deny list")
    # `allow` as an actual allow-list, not merely a scoring hint. Skipped when `allow` is
    # empty, which is the "I have not thought about location" case and must stay permissive.
    if profile.locations.allow and location_match(posting, profile) == 0.0:
        where = ", ".join(posting.locations) or "no location stated"
        lower(_disposition_ceiling(profile.locations.on_mismatch),
              f"location {where!r} matches neither allow nor prefer -> "
              f"{profile.locations.on_mismatch}")

    # --- role --------------------------------------------------------------------------
    role_class = analysis.role_class
    if role_class == "unclear":
        lower(_disposition_ceiling(profile.roles.on_unclear_role),
              f"unclear role -> {profile.roles.on_unclear_role}")
    elif role_class not in profile.roles.role_class:
        lower("silent", f"role_class {role_class!r} not wanted by this profile")
    if analysis.degree_gated:
        lower(_disposition_ceiling(profile.roles.degree_gated),
              f"degree-gated -> {profile.roles.degree_gated}")

    # --- work authorization -------------------------------------------------------------
    elig = profile.eligibility
    if elig.needs_sponsorship:
        # Clearance and ITAR are the only true hard blocks in this system, which is why
        # they get a toggle of their own rather than riding on `on_blocked`: you may well
        # want to eyeball a `blocked` sponsorship-refusal posting and never want to see a
        # TS/SCI req again.
        hard = {"clearance", "export_control"}.intersection(analysis.blocking_groups)
        if hard and elig.suppress_clearance:
            lower("silent", f"hard block: {', '.join(sorted(hard))}")
        disposition = {
            "blocked": elig.on_blocked,
            "likely_ok": elig.on_likely_ok,
            "unknown": elig.on_unknown,
        }[analysis.eligibility]
        lower(_disposition_ceiling(disposition),
              f"eligibility {analysis.eligibility} -> {disposition}")
        if not confidence_meets(analysis.sponsor_confidence, elig.min_sponsor_confidence):
            if elig.on_below_min_sponsor == "suppress":
                lower("silent", "sponsor confidence below minimum")
            elif elig.on_below_min_sponsor == "digest":
                lower("digest", "sponsor confidence below minimum")

    # --- bookkeeping ---------------------------------------------------------------------
    if profile.limits.suppress_if_applied and posting.global_id in (applied or set()):
        lower("silent", "already applied")

    return ceiling, reasons


def route(
    posting: Posting,
    analysis: Analysis,
    profile: Profile,
    tier: set[str] | None = None,
    applied: set[str] | None = None,
) -> Candidate:
    """Score one posting and decide where it goes, for one profile."""
    value, breakdown = score(posting, analysis, profile, tier)
    ceiling, reasons = policy_ceiling(posting, analysis, profile, applied)

    if value >= INSTANT_THRESHOLD or posting.company in (tier or set()):
        proposed: Route = "instant"
    elif value >= profile.limits.min_score:
        proposed = "digest"
    else:
        proposed = "silent"
        reasons.append(f"score {value:.2f} below min_score {profile.limits.min_score:.2f}")

    final = _quieter(proposed, ceiling)
    return Candidate(
        posting=posting,
        analysis=analysis,
        score=value,
        route=final,
        group=profile.roles.priorities.group_for(list(analysis.function)),
        reasons=reasons,
        breakdown=breakdown,
    )


# --- caps --------------------------------------------------------------------------------

@dataclass(slots=True)
class Capped:
    """What survived the caps, and what did not. The held-back counts are reported, never
    silently dropped (T8.6, T8.7)."""

    instant: list[Candidate] = field(default_factory=list)
    digest: list[Candidate] = field(default_factory=list)
    silent: list[Candidate] = field(default_factory=list)
    truncated_digest: int = 0
    truncated_instant: int = 0
    held_by_company: dict[str, int] = field(default_factory=dict)

    @property
    def sendable(self) -> list[Candidate]:
        return [*self.instant, *self.digest]


def apply_caps(
    candidates: list[Candidate],
    profile: Profile,
    already_today: dict[str, int] | None = None,
    already_by_company: dict[str, int] | None = None,
) -> Capped:
    """Enforce `limits`, highest score first.

    The caps are the hard guarantee: even if every threshold is miscalibrated, you cannot
    receive more than `max_instant_per_day` plus two digests. Everything cut here is still
    stored and still visible via `jobmon list --all` - a cap changes what reaches your
    phone, never what the system knows.

    `already_today` / `already_by_company` carry counts from earlier runs of the same day,
    because an hourly cron would otherwise reset a per-DAY cap twenty-four times.
    """
    limits = profile.limits
    already_today = dict(already_today or {})
    per_company = dict(already_by_company or {})
    out = Capped()

    ordered = sorted(candidates, key=lambda c: (-c.score, c.global_id))
    for cand in ordered:
        if cand.route == "silent":
            out.silent.append(cand)
            continue

        key = normalize_company(cand.posting.company)
        company_cap = limits.max_per_company_per_day
        if company_cap is not None and per_company.get(key, 0) >= company_cap:
            out.held_by_company[cand.posting.company] = (
                out.held_by_company.get(cand.posting.company, 0) + 1
            )
            cand.reasons.append(
                f"held back: {company_cap}/day already sent for {cand.posting.company}"
            )
            out.silent.append(cand)
            continue

        target = cand.route
        instant_cap = limits.max_instant_per_day
        over_instant_budget = (
            instant_cap is not None and already_today.get("instant", 0) >= instant_cap
        )
        if target == "instant" and over_instant_budget:
            # Demote rather than drop. The daily instant budget is about interruption,
            # not about relevance, and the posting is still worth a digest line.
            target = "digest"
            out.truncated_instant += 1
            cand.reasons.append(
                f"instant budget of {instant_cap}/day spent; demoted to digest"
            )

        digest_cap = limits.max_digest_items
        if target == "digest" and digest_cap is not None and len(out.digest) >= digest_cap:
            out.truncated_digest += 1
            cand.reasons.append(f"digest capped at {digest_cap} items this run")
            out.silent.append(cand)
            continue

        cand.route = target
        (out.instant if target == "instant" else out.digest).append(cand)
        already_today[target] = already_today.get(target, 0) + 1
        per_company[key] = per_company.get(key, 0) + 1

    return out


# --- Phase X: sort modes -------------------------------------------------------------------

def sort_candidates(candidates: list[Candidate], profile: Profile) -> list[Candidate]:
    """Order a feed according to `priorities.sort_by`.

    | mode     | order                                                    |
    |----------|----------------------------------------------------------|
    | priority | grouped by category in your listed order, score within   |
    | score    | pure fit score, categories interleaved                   |
    | date     | newest first, priority ignored                           |
    | company  | grouped by employer                                      |

    KNOWN TRADEOFF, and it is intended behaviour: `priority` groups BEFORE it scores, so a
    strong `swe` match sits below mediocre `mle` ones. If you find yourself routinely
    scrolling past the first group, switch to `score` and let `order` act as a tiebreaker.
    The scrolling is the signal; re-ranking `order` is not the fix.
    """
    mode = profile.roles.priorities.sort_by
    resolved = profile.roles.priorities.resolved
    floor = rank_weights.FLOOR

    def posted(c: Candidate) -> float:
        when: datetime = c.posting.posted_at or c.posting.first_seen_at
        return when.timestamp()

    if mode == "score":
        return sorted(candidates, key=lambda c: (-c.score, c.global_id))
    if mode == "date":
        return sorted(candidates, key=lambda c: (-posted(c), c.global_id))
    if mode == "company":
        return sorted(
            candidates,
            key=lambda c: (normalize_company(c.posting.company), -c.score, c.global_id),
        )
    # priority
    return sorted(
        candidates,
        key=lambda c: (-resolved.get(c.group, floor), c.group, -c.score, c.global_id),
    )
