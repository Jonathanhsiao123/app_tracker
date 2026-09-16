"""User profiles: everything subjective, per user (addendum 3, Phase X).

    fetch -> normalize -> store -> ANALYZE (objective) ->  [profile A] -> alerts
                                                           [profile B] -> alerts

Analysis is a set of facts about a posting - eligibility label, evidence sentence, keyword
matches, degree flag, YoE floor, role class, function, locations - computed once and shared.
A profile is a set of opinions about those facts: what to suppress, what to instant-alert,
what a good fit is, how many alerts a day is tolerable. Nothing in this module touches a
posting record, which is why adding a second user costs zero extra fetching and zero extra
analysis, and why changing your own preferences never requires a re-crawl.

VALIDATION POLICY, and the reason it is strict (T8.12). `config_loader` splits YAML errors
into "would silently shrink the sweep" (raise) and "only costs signal" (warn). A profile
inverts the stakes: the dangerous direction is not a smaller sweep, it is a config typo
that drops the whole noise-control layer and restores the 400-alerts-a-week flood you
edited the file to stop. So an unknown key, a misspelled enum value, or a category in both
`order` and `never` all RAISE, naming the field. There is no fall-back-to-permissive path
anywhere in this file.

Every field has a default, so a minimal profile is four lines (T8.9). The defaults are
deliberately the PRE-ADDENDUM behaviour - unclear roles alert, unknown eligibility alerts,
no caps - so that installing this code changes nothing until you write a profile that asks
for something different (build order step 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config_loader import ConfigError, rank_weights
from .models import SponsorConfidence

log = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).parent / "config" / "profiles"
DEFAULT_PROFILE_NAME = "default"

# --- vocabularies. A value outside one of these is a typo, and a typo is an error. ----

DISPOSITIONS = ("suppress", "digest", "alert")
SORT_MODES = ("priority", "score", "date", "company")
ROLE_CLASSES = ("new_grad", "internship", "experienced", "unclear")
CHANNELS = ("ntfy", "email", "all", "none")
CONFIDENCE_ORDER: dict[str, int] = {
    "none_observed": 0, "unknown": 1, "occasional": 2, "frequent": 3,
}
MIN_CONFIDENCE_VALUES = (*CONFIDENCE_ORDER, "any")

# Addendum 4: "frequent=1.0 ... none_observed=0.3". `unknown` sits between the two
# observed tiers rather than at the bottom, because no DOL match is overwhelmingly a
# fuzzy-name miss or a small employer, not evidence of refusal.
CONFIDENCE_SCORE: dict[str, float] = {
    "frequent": 1.0, "occasional": 0.7, "unknown": 0.5, "none_observed": 0.3,
}


class ProfileError(ConfigError):
    """A profile YAML file is wrong. Always names the offending field (T8.12)."""


# --- blocks ---------------------------------------------------------------------------

@dataclass(slots=True)
class Eligibility:
    """Work authorization. The whole block no-ops when `needs_sponsorship` is false, which
    is what a citizen/PR user's profile says (T8.2) - including suppressing the VISA badge,
    since a badge about a constraint you do not have is pure noise."""

    needs_sponsorship: bool = True
    on_blocked: str = "suppress"
    on_unknown: str = "alert"        # addendum default is `digest`; see module docstring
    on_likely_ok: str = "alert"
    min_sponsor_confidence: str = "any"
    suppress_clearance: bool = True
    # Addendum 4: "`min_sponsor_confidence` should downrank, never suppress, unless you
    # explicitly set it to suppress." This is that explicit setting, kept as its own field
    # so that `min_sponsor_confidence` itself cannot be made to suppress by accident.
    on_below_min_sponsor: str = "downrank"  # downrank | digest | suppress


@dataclass(slots=True)
class Priorities:
    """Category ranking (Phase X). An ordered list, not a dict of floats.

    Tuning six floats to produce a ranking you already know in your head is fiddly, easy to
    get subtly wrong, and produces no error when you do. An ordered list carries the same
    information and is edited by moving one line. `weights` remains as the escape hatch for
    the one case ordering cannot express: spacing.
    """

    order: list[str] = field(default_factory=list)
    never: list[str] = field(default_factory=list)
    sort_by: str = "priority"
    weights: dict[str, float] = field(default_factory=dict)  # overrides, by category
    # Rank-derived, filled in by the loader. Read by scoring; never edited by hand.
    resolved: dict[str, float] = field(default_factory=dict)

    def weight_for(self, categories: list[str]) -> float:
        """The weight of the highest-ranked category this posting matches.

        A posting matching several categories takes its highest-ranked one, and that same
        weight is what scoring uses - so grouping and ranking cannot disagree (TX.6).
        """
        if not categories:
            return rank_weights.FLOOR
        return max(self.resolved.get(c, rank_weights.FLOOR) for c in categories)

    def group_for(self, categories: list[str]) -> str:
        """The category a posting is grouped under in `sort_by: priority` (TX.6)."""
        if not categories:
            return "unclassified"
        return max(categories, key=lambda c: (self.resolved.get(c, rank_weights.FLOOR), c))


@dataclass(slots=True)
class Roles:
    """What counts as relevant."""

    role_class: list[str] = field(default_factory=lambda: ["new_grad", "internship"])
    on_unclear_role: str = "alert"   # addendum default is `digest`; see module docstring
    degree_gated: str = "alert"
    max_yoe: int = 2
    priorities: Priorities = field(default_factory=Priorities)


@dataclass(slots=True)
class Locations:
    allow: list[str] = field(default_factory=list)   # empty = everywhere allowed
    prefer: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Limits:
    """Noise control. These caps, not the thresholds, are the hard guarantee: even if every
    threshold above is miscalibrated, you cannot receive more than `max_instant_per_day`
    plus two digests. `None` means uncapped, which is the pre-addendum behaviour."""

    min_score: float = 0.0
    max_instant_per_day: int | None = None
    max_digest_items: int | None = None
    max_per_company_per_day: int | None = None
    suppress_if_applied: bool = True
    suppress_reposts: bool = True


@dataclass(slots=True)
class Denylist:
    companies: list[str] = field(default_factory=list)
    title_terms: list[str] = field(default_factory=list)
    # Individual postings, appended by `jobmon mute`. Merged in from the feedback store at
    # load time rather than written into the YAML, so muting never edits a file you hand-
    # maintain (addendum 6, T8.11).
    postings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Routing:
    instant: str = "all"
    digest: str = "all"
    silent: str = "none"


@dataclass(slots=True)
class Scoring:
    """Addendum 5's formula, term by term. Defaults sum to 1.0 so that `min_score: 0.55`
    and the 0.8 instant threshold mean what they look like they mean."""

    w_function: float = 0.35
    w_keywords: float = 0.25
    w_location: float = 0.15
    w_eligibility: float = 0.15
    w_tier: float = 0.10
    penalty_degree_gated: float = 0.15
    penalty_yoe_over: float = 0.25
    penalty_below_min_sponsor: float = 0.10
    boost_bonus: float = 0.25  # `jobmon boost <global_id>`


@dataclass(slots=True)
class Profile:
    """One user's opinions. Never written to; loaded fresh every run so that editing the
    YAML and saving is the entire deployment process (Phase X, "Changing it")."""

    name: str = DEFAULT_PROFILE_NAME
    locale: str = "us"
    eligibility: Eligibility = field(default_factory=Eligibility)
    roles: Roles = field(default_factory=Roles)
    locations: Locations = field(default_factory=Locations)
    limits: Limits = field(default_factory=Limits)
    denylist: Denylist = field(default_factory=Denylist)
    routing: Routing = field(default_factory=Routing)
    scoring: Scoring = field(default_factory=Scoring)
    boosted: list[str] = field(default_factory=list)  # from the feedback store
    path: Path | None = None


# --- validation helpers ---------------------------------------------------------------

def _block(raw: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ProfileError(f"{where}: {key!r} must be a mapping, got {type(value).__name__}")
    return value


def _reject_unknown(raw: dict[str, Any], known: tuple[str, ...], where: str) -> None:
    """A key nobody reads is a preference that silently does nothing. Name it and stop.

    This is the guard behind T8.12: `on_unkown: digest` would otherwise load cleanly, leave
    `on_unknown` at its permissive default, and restore the flood - with no error anywhere.
    """
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ProfileError(
            f"{where}: unknown field(s) {', '.join(repr(k) for k in unknown)}; "
            f"known: {', '.join(sorted(known))}"
        )


def _enum(value: Any, allowed: tuple[str, ...], where: str) -> str:
    if value not in allowed:
        raise ProfileError(
            f"{where}: {value!r} is not one of {', '.join(sorted(allowed))}"
        )
    return str(value)


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ProfileError(f"{where}: must be true or false, got {value!r}")
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{where}: must be a number, got {value!r}")
    return float(value)


def _positive_int_or_none(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProfileError(f"{where}: must be a non-negative whole number or null, got {value!r}")
    return value


def _str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ProfileError(f"{where}: must be a list, got {value!r}")
    return [str(v) for v in value]


# --- block parsers ----------------------------------------------------------------------

def _parse_eligibility(raw: dict[str, Any], where: str) -> Eligibility:
    known = ("needs_sponsorship", "on_blocked", "on_unknown", "on_likely_ok",
             "min_sponsor_confidence", "suppress_clearance", "on_below_min_sponsor")
    _reject_unknown(raw, known, where)
    out = Eligibility()
    if "needs_sponsorship" in raw:
        out.needs_sponsorship = _bool(raw["needs_sponsorship"], f"{where}.needs_sponsorship")
    for key in ("on_blocked", "on_unknown", "on_likely_ok"):
        if key in raw:
            setattr(out, key, _enum(raw[key], DISPOSITIONS, f"{where}.{key}"))
    if "min_sponsor_confidence" in raw:
        out.min_sponsor_confidence = _enum(
            raw["min_sponsor_confidence"], MIN_CONFIDENCE_VALUES,
            f"{where}.min_sponsor_confidence",
        )
    if "suppress_clearance" in raw:
        out.suppress_clearance = _bool(raw["suppress_clearance"], f"{where}.suppress_clearance")
    if "on_below_min_sponsor" in raw:
        out.on_below_min_sponsor = _enum(
            raw["on_below_min_sponsor"], ("downrank", "digest", "suppress"),
            f"{where}.on_below_min_sponsor",
        )
    return out


def _parse_priorities(raw: dict[str, Any], where: str) -> Priorities:
    _reject_unknown(raw, ("order", "never", "sort_by", "weights"), where)
    order = _str_list(raw.get("order"), f"{where}.order")
    never = _str_list(raw.get("never"), f"{where}.never")

    # TX.5. A category in both lists is the only way this config can contradict itself,
    # and either resolution is defensible - which is exactly why the code must not pick
    # one. Silently ranking a category you told it never to alert, or silently dropping a
    # category you ranked first, are both failures you would not notice for weeks.
    both = sorted(set(order) & set(never))
    if both:
        raise ProfileError(
            f"{where}: category/ies {', '.join(repr(c) for c in both)} appear in both "
            f"'order' and 'never'. Remove from one - this cannot be resolved for you"
        )
    duplicates = sorted({c for c in order if order.count(c) > 1})
    if duplicates:
        raise ProfileError(
            f"{where}.order: {', '.join(repr(c) for c in duplicates)} listed more than "
            f"once; rank position would be ambiguous"
        )

    overrides: dict[str, float] = {}
    for cat, value in _block(raw, "weights", where).items():
        overrides[str(cat)] = _number(value, f"{where}.weights.{cat}")

    sort_by = _enum(raw.get("sort_by", "priority"), SORT_MODES, f"{where}.sort_by")
    if not order:
        # TX.9. Legal, and occasionally what you want ("rank nothing, just score"), but it
        # silently disables the whole feature, so say so out loud once per run.
        log.warning(
            "%s.order is empty: every category takes the floor weight %.2f and "
            "`sort_by: priority` will group everything together",
            where, rank_weights.FLOOR,
        )
    return Priorities(
        order=order, never=never, sort_by=sort_by, weights=overrides,
        resolved=rank_weights(order, overrides),
    )


def _parse_roles(raw: dict[str, Any], where: str) -> Roles:
    _reject_unknown(
        raw, ("role_class", "on_unclear_role", "degree_gated", "max_yoe", "priorities"), where
    )
    out = Roles()
    if "role_class" in raw:
        classes = _str_list(raw["role_class"], f"{where}.role_class")
        for c in classes:
            _enum(c, ROLE_CLASSES, f"{where}.role_class")
        out.role_class = classes
    for key in ("on_unclear_role", "degree_gated"):
        if key in raw:
            setattr(out, key, _enum(raw[key], DISPOSITIONS, f"{where}.{key}"))
    if "max_yoe" in raw:
        out.max_yoe = int(_number(raw["max_yoe"], f"{where}.max_yoe"))
    out.priorities = _parse_priorities(_block(raw, "priorities", where), f"{where}.priorities")
    return out


def _parse_locations(raw: dict[str, Any], where: str) -> Locations:
    _reject_unknown(raw, ("allow", "prefer", "deny"), where)
    return Locations(
        allow=_str_list(raw.get("allow"), f"{where}.allow"),
        prefer=_str_list(raw.get("prefer"), f"{where}.prefer"),
        deny=_str_list(raw.get("deny"), f"{where}.deny"),
    )


def _parse_limits(raw: dict[str, Any], where: str) -> Limits:
    known = ("min_score", "max_instant_per_day", "max_digest_items",
             "max_per_company_per_day", "suppress_if_applied", "suppress_reposts")
    _reject_unknown(raw, known, where)
    out = Limits()
    if "min_score" in raw:
        out.min_score = _number(raw["min_score"], f"{where}.min_score")
    for key in ("max_instant_per_day", "max_digest_items", "max_per_company_per_day"):
        if key in raw:
            setattr(out, key, _positive_int_or_none(raw[key], f"{where}.{key}"))
    for key in ("suppress_if_applied", "suppress_reposts"):
        if key in raw:
            setattr(out, key, _bool(raw[key], f"{where}.{key}"))
    return out


def _parse_denylist(raw: dict[str, Any], where: str) -> Denylist:
    _reject_unknown(raw, ("companies", "title_terms", "postings"), where)
    return Denylist(
        companies=_str_list(raw.get("companies"), f"{where}.companies"),
        title_terms=_str_list(raw.get("title_terms"), f"{where}.title_terms"),
        postings=_str_list(raw.get("postings"), f"{where}.postings"),
    )


def _parse_routing(raw: dict[str, Any], where: str) -> Routing:
    _reject_unknown(raw, ("instant", "digest", "silent"), where)
    out = Routing()
    for key in ("instant", "digest", "silent"):
        if key in raw:
            setattr(out, key, _enum(raw[key], CHANNELS, f"{where}.{key}"))
    return out


def _parse_scoring(raw: dict[str, Any], where: str) -> Scoring:
    out = Scoring()
    known = tuple(f.name for f in out.__dataclass_fields__.values())  # type: ignore[attr-defined]
    _reject_unknown(raw, known, where)
    for key in known:
        if key in raw:
            setattr(out, key, _number(raw[key], f"{where}.{key}"))
    return out


# --- loading ---------------------------------------------------------------------------

TOP_LEVEL = ("name", "locale", "eligibility", "roles", "locations", "limits",
             "denylist", "routing", "scoring")


def parse(raw: dict[str, Any] | None, name: str, path: Path | None = None) -> Profile:
    """Build one Profile from already-parsed YAML. Raises `ProfileError`, never falls back.

    `name` is the filename stem and wins over any `name:` inside the file: the stem is what
    `--profile` takes, what `alerted/<profile>.jsonl` is called, and what the `alerts` table
    records, so letting the two disagree would split one user's history across two keys.
    """
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ProfileError(f"profile {name!r}: top level must be a mapping, got "
                           f"{type(raw).__name__}")
    _reject_unknown(raw, TOP_LEVEL, f"profile {name!r}")
    inner = raw.get("name")
    if inner is not None and str(inner) != name:
        log.warning(
            "profile %s: 'name: %s' inside the file disagrees with the filename; "
            "the filename wins (it is what --profile and the alert ledger use)",
            name, inner,
        )
    where = f"profile {name!r}"
    return Profile(
        name=name,
        locale=str(raw.get("locale", "us")),
        eligibility=_parse_eligibility(_block(raw, "eligibility", where), f"{where}.eligibility"),
        roles=_parse_roles(_block(raw, "roles", where), f"{where}.roles"),
        locations=_parse_locations(_block(raw, "locations", where), f"{where}.locations"),
        limits=_parse_limits(_block(raw, "limits", where), f"{where}.limits"),
        denylist=_parse_denylist(_block(raw, "denylist", where), f"{where}.denylist"),
        routing=_parse_routing(_block(raw, "routing", where), f"{where}.routing"),
        scoring=_parse_scoring(_block(raw, "scoring", where), f"{where}.scoring"),
        path=path,
    )


def load_profile(path: Path) -> Profile:
    """Read and validate one `config/profiles/<name>.yaml`.

    A YAML syntax error is re-raised as `ProfileError` with the file named. The run stops:
    a profile that will not parse must never degrade into "alert everything" (T8.12).
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path}: not valid YAML: {exc}") from exc
    return parse(raw, name=path.stem, path=path)


def merge_feedback(profile: Profile, feedback_dir: Path | None = None) -> Profile:
    """Fold `jobmon mute` / `mute-company` / `boost` into a loaded profile, in place.

    The denylist a run actually enforces is the hand-written YAML plus everything you have
    muted since; keeping the two apart on disk and joining them here is what lets muting be
    instant without rewriting a commented config file (addendum 6, T8.11).
    """
    from . import feedback  # local: feedback imports nothing from here, but keep it one-way

    directory = feedback_dir or feedback.FEEDBACK_DIR
    muted, companies, boosted = feedback.for_profile(profile.name, directory)
    for gid in muted:
        if gid not in profile.denylist.postings:
            profile.denylist.postings.append(gid)
    for company in companies:
        if company not in profile.denylist.companies:
            profile.denylist.companies.append(company)
    profile.boosted = boosted
    return profile


def load_all(
    directory: Path | None = None, feedback_dir: Path | None = None
) -> dict[str, Profile]:
    """Every profile in `directory`, keyed by filename stem, with feedback merged in.

    A directory with no profiles yields the built-in default, so a fresh checkout still
    runs and still behaves exactly as it did before the addendum.
    """
    # Read the module attribute here, not in the signature: a default argument binds at
    # def time and could not then be redirected by a test or a future --profiles-dir flag.
    directory = directory or PROFILES_DIR
    if not directory.is_dir():
        found = {DEFAULT_PROFILE_NAME: Profile()}
    else:
        found = {p.stem: load_profile(p) for p in sorted(directory.glob("*.yaml"))}
        found = found or {DEFAULT_PROFILE_NAME: Profile()}
    return {name: merge_feedback(p, feedback_dir) for name, p in found.items()}


def select(
    directory: Path | None = None,
    only: str | None = None,
    feedback_dir: Path | None = None,
) -> list[Profile]:
    """The profiles one run should deliver for. `--profile <name>`, or all of them.

    An unknown `--profile` is an error listing what does exist; silently running zero
    profiles would look exactly like a quiet day on the boards.
    """
    directory = directory or PROFILES_DIR
    profiles = load_all(directory, feedback_dir)
    if only is None:
        return list(profiles.values())
    if only not in profiles:
        raise ProfileError(
            f"no profile {only!r} in {directory}; found: {', '.join(sorted(profiles)) or 'none'}"
        )
    return [profiles[only]]


def confidence_meets(actual: str, minimum: str) -> bool:
    """Is `actual` at least `minimum`? `any` is always met."""
    if minimum == "any":
        return True
    return CONFIDENCE_ORDER.get(actual, 1) >= CONFIDENCE_ORDER.get(minimum, 0)


def confidence_score(confidence: SponsorConfidence | str) -> float:
    return CONFIDENCE_SCORE.get(str(confidence), 0.5)
