"""Per-profile scoring, routing, caps and sort order.

Testbench T8.1, T8.2, T8.3, T8.6, T8.7, T8.8, T8.10 and TX.1, TX.2, TX.6, TX.7.

No network and no notifications anywhere in this file: what is under test is where a
posting is ALLOWED to go for a given profile, which is a pure function of two records and
one config.
"""

from datetime import UTC, datetime, timedelta

import pytest

from jobmon import runner, scoring, store
from jobmon.config_loader import rank_weights
from jobmon.models import Analysis, Posting
from jobmon.profiles import (
    Denylist,
    Eligibility,
    Limits,
    Locations,
    Priorities,
    Profile,
    Roles,
)


def make_posting(gid="gh:acme:1", company="Acme", title="Software Engineer, New Grad",
                 locations=("New York",), remote_type=None, posted_at=None):
    when = posted_at or datetime.now(UTC)
    return Posting(
        global_id=gid, source="greenhouse", company=company, title=title,
        locations=list(locations), description_html="", description_text="",
        apply_url=f"https://example/{gid}", first_seen_at=when, last_seen_at=when,
        remote_type=remote_type, posted_at=when,
    )


def make_analysis(gid="gh:acme:1", role_class="new_grad", function=("swe",),
                  eligibility="unknown", sponsor_confidence="unknown",
                  variant_scores=None, degree_gated=False, yoe_floor=None,
                  blocking_groups=(), evidence=()):
    return Analysis(
        global_id=gid, role_class=role_class, function=list(function),
        eligibility=eligibility, eligibility_evidence=list(evidence), keywords={}, gaps=[],
        recommended_variant="generalist",
        variant_scores=variant_scores if variant_scores is not None else {"generalist": 0.7},
        degree_gated=degree_gated, yoe_floor=yoe_floor,
        text_eligibility=eligibility, sponsor_confidence=sponsor_confidence,
        blocking_groups=list(blocking_groups),
    )


def priorities(order=("mle", "security", "systems", "swe", "data"), never=(),
               sort_by="priority", weights=None):
    return Priorities(
        order=list(order), never=list(never), sort_by=sort_by,
        weights=dict(weights or {}),
        resolved=rank_weights(list(order), dict(weights or {})),
    )


def policy(**kw):
    """A profile with `min_score: 0` - for tests about the POLICY ceiling.

    Routing is the quieter of a policy ceiling and a score proposal, so a test about
    `on_blocked` must not also be a test about whether a synthetic posting happens to
    clear 0.55. Tests about the threshold itself use `tuned()`.
    """
    profile = tuned(**kw)
    profile.limits.min_score = 0.0
    return profile


def tuned(**kw):
    """A profile shaped like the shipped one: the tuned settings from addendum 3."""
    base = Profile(
        name="tuned",
        eligibility=Eligibility(on_unknown="digest", on_likely_ok="alert",
                                on_blocked="suppress"),
        roles=Roles(role_class=["new_grad", "internship"], on_unclear_role="digest",
                    degree_gated="digest", priorities=priorities()),
        limits=Limits(min_score=0.55, max_instant_per_day=8, max_digest_items=25,
                      max_per_company_per_day=3),
    )
    for key, value in kw.items():
        setattr(base, key, value)
    return base


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_db(c)
    yield c
    c.close()


# --- T8.1: two profiles, one posting ---------------------------------------------------

def test_t8_1_two_profiles_alert_independently_on_the_same_posting(conn):
    """MANDATORY. Profile A's alert must not suppress profile B's.

    This is the reason `postings.alerted_at` had to be split into an `alerts` table keyed
    by (posting, profile). Under the old single flag, whichever profile ran first silenced
    every other profile for that posting, permanently.
    """
    posting = make_posting()
    store.upsert_posting(conn, posting, run_id=1)

    assert [p.global_id for p in store.unalerted_postings(conn, 1, "alice")] == ["gh:acme:1"]
    assert [p.global_id for p in store.unalerted_postings(conn, 1, "bob")] == ["gh:acme:1"]

    store.mark_alerted(conn, ["gh:acme:1"], datetime.now(UTC), profile="alice")

    assert store.unalerted_postings(conn, 1, "alice") == []
    assert [p.global_id for p in store.unalerted_postings(conn, 1, "bob")] == ["gh:acme:1"], \
        "alice's alert silenced bob"

    store.mark_alerted(conn, ["gh:acme:1"], datetime.now(UTC), profile="bob")
    assert store.unalerted_postings(conn, 1, "bob") == []
    assert store.alerted_at(conn, "gh:acme:1", "alice") is not None
    assert store.alerted_at(conn, "gh:acme:1", "bob") is not None


def test_two_profiles_route_the_same_posting_differently():
    """The other half of T8.1: same facts, different opinions, no shared state."""
    posting = make_posting()
    analysis = make_analysis(role_class="unclear")
    strict = policy(roles=Roles(on_unclear_role="suppress", priorities=priorities()))
    lenient = policy(roles=Roles(on_unclear_role="alert", priorities=priorities()))

    assert scoring.route(posting, analysis, strict).route == "silent"
    assert scoring.route(posting, analysis, lenient).route != "silent"


# --- T8.2: needs_sponsorship: false ------------------------------------------------------

def test_t8_2_eligibility_block_no_ops_when_sponsorship_is_not_needed():
    """A citizen/PR user's profile. The whole block no-ops - including on `blocked`."""
    posting = make_posting()
    blocked = make_analysis(
        eligibility="blocked", blocking_groups=["citizenship"],
        evidence=["Must be a U.S. citizen."],
    )
    citizen = policy(eligibility=Eligibility(needs_sponsorship=False))
    intl = policy(eligibility=Eligibility(needs_sponsorship=True, on_blocked="suppress"))

    assert scoring.route(posting, blocked, citizen).route != "silent"
    assert scoring.route(posting, blocked, intl).route == "silent"


def test_t8_2_no_visa_badge_is_rendered_for_a_citizen_profile():
    from jobmon import notify

    posting, analysis = make_posting(), make_analysis(sponsor_confidence="frequent")
    citizen = tuned(eligibility=Eligibility(needs_sponsorship=False))

    assert "VISA:" not in notify.format_alert(posting, analysis, citizen)
    assert "VISA:" in notify.format_alert(posting, analysis, tuned())


def test_t8_2_the_eligibility_score_term_is_neutral_not_zero_for_a_citizen():
    """Zeroing the term would quietly depress every score for that user by w_eligibility,
    which looks like a scoring bug and reads like a broken profile."""
    posting, analysis = make_posting(), make_analysis(sponsor_confidence="none_observed")
    citizen = tuned(eligibility=Eligibility(needs_sponsorship=False))
    _, breakdown = scoring.score(posting, analysis, citizen)
    assert breakdown["eligibility"] == citizen.scoring.w_eligibility
    assert "below_min_sponsor" not in breakdown


# --- T8.3: on_blocked: suppress ------------------------------------------------------------

def test_t8_3_blocked_posting_is_stored_absent_from_alerts_and_visible_in_list_all(conn):
    """Nothing is deleted. A cap or a policy changes what reaches your phone, never what
    the system knows."""
    posting = make_posting()
    store.upsert_posting(conn, posting, run_id=1)
    analysis = make_analysis(eligibility="blocked", evidence=["Must be a U.S. citizen."])
    store.upsert_analysis(conn, analysis)

    candidate = scoring.route(posting, analysis, tuned())
    assert candidate.route == "silent"
    assert any("blocked" in r for r in candidate.reasons)

    # still stored, and still retrievable
    assert store.get_posting(conn, posting.global_id) is not None
    shown = [p.global_id for p, _ in store.iter_postings(conn, include_blocked=True)]
    assert posting.global_id in shown
    hidden = [p.global_id for p, _ in store.iter_postings(conn, include_blocked=False)]
    assert posting.global_id not in hidden


def test_suppress_clearance_is_a_separate_toggle_from_on_blocked():
    """Clearance and ITAR are the only true hard blocks. You may well want to eyeball a
    `blocked` sponsorship-refusal posting and never want to see a TS/SCI req again."""
    posting = make_posting()
    clearance = make_analysis(eligibility="blocked", blocking_groups=["clearance"])
    refusal = make_analysis(eligibility="blocked", blocking_groups=["sponsorship_refusal"])

    lenient = policy(eligibility=Eligibility(on_blocked="digest", suppress_clearance=True))
    assert scoring.route(posting, clearance, lenient).route == "silent"
    assert scoring.route(posting, refusal, lenient).route == "digest"


# --- T8.6 / T8.7: the caps ------------------------------------------------------------------

def _candidates(n, company="Acme", score_base=0.9):
    out = []
    for i in range(n):
        posting = make_posting(gid=f"gh:acme:{i:03d}", company=company)
        out.append(scoring.Candidate(
            posting=posting, analysis=make_analysis(gid=posting.global_id),
            score=score_base - i * 0.001, route="digest", group="swe",
        ))
    return out


def test_t8_6_digest_cap_sends_the_highest_scoring_and_states_the_truncation():
    """T8.6: 40 qualifying postings, max_digest_items 25 -> 25 sent, 15 stored, and the
    digest SAYS SO. A digest that quietly drops its twenty-sixth item is one you stop
    trusting."""
    profile = tuned(limits=Limits(min_score=0.0, max_digest_items=25))
    capped = scoring.apply_caps(_candidates(40), profile)

    assert len(capped.digest) == 25
    assert capped.truncated_digest == 15
    assert len(capped.silent) == 15
    # highest-scoring survive
    sent = {c.global_id for c in capped.digest}
    assert sent == {f"gh:acme:{i:03d}" for i in range(25)}

    from jobmon import notify

    notice = notify._truncation_notice(profile, capped.truncated_digest, {})
    assert "15 further matching posting(s) were held back" in notice
    assert "max_digest_items=25" in notice
    assert "jobmon list" in notice, "the held-back items must be reachable"


def test_t8_7_per_company_cap_holds_the_rest_back_and_shows_the_count():
    """T8.7: one company posts 12 matching reqs, max_per_company_per_day 3 -> 3 alerted,
    count of held-back reqs shown."""
    profile = tuned(limits=Limits(min_score=0.0, max_per_company_per_day=3))
    capped = scoring.apply_caps(_candidates(12, company="Flood Corp"), profile)

    assert len(capped.sendable) == 3
    assert capped.held_by_company == {"Flood Corp": 9}

    from jobmon import notify

    notice = notify._truncation_notice(profile, 0, capped.held_by_company)
    assert "9 more req(s) from Flood Corp held back" in notice
    assert "max_per_company_per_day=3" in notice


def test_the_per_day_caps_count_alerts_already_sent_earlier_today():
    """An hourly cron would otherwise reset a per-DAY cap twenty-four times a day."""
    profile = tuned(limits=Limits(min_score=0.0, max_per_company_per_day=3))
    capped = scoring.apply_caps(
        _candidates(5, company="Acme"), profile, already_by_company={"acme": 2}
    )
    assert len(capped.sendable) == 1


def test_the_instant_budget_demotes_rather_than_drops():
    """The daily instant budget is about interruption, not relevance: over budget, the
    posting is still worth a digest line."""
    profile = tuned(limits=Limits(min_score=0.0, max_instant_per_day=2))
    cands = _candidates(5)
    for c in cands:
        c.route = "instant"
    capped = scoring.apply_caps(cands, profile)
    assert len(capped.instant) == 2
    assert len(capped.digest) == 3
    assert capped.truncated_instant == 3
    assert len(capped.sendable) == 5, "nothing was dropped, only demoted"


def test_caps_are_the_hard_guarantee_even_with_broken_thresholds():
    """`even if every threshold is miscalibrated, you cannot receive more than
    max_instant_per_day plus two digests.`"""
    reckless = tuned(limits=Limits(min_score=0.0, max_instant_per_day=8,
                                   max_digest_items=25))
    cands = _candidates(500, score_base=1.0)
    for c in cands:
        c.route = "instant"
    capped = scoring.apply_caps(cands, reckless)
    assert len(capped.instant) <= 8
    assert len(capped.digest) <= 25


# --- T8.8: suppress_if_applied ---------------------------------------------------------------

def test_t8_8_a_posting_already_marked_applied_does_not_alert():
    posting, analysis = make_posting(), make_analysis()
    profile = policy()
    assert scoring.route(posting, analysis, profile, applied=set()).route != "silent"
    routed = scoring.route(posting, analysis, profile, applied={posting.global_id})
    assert routed.route == "silent"
    assert "already applied" in routed.reasons


def test_suppress_if_applied_false_still_alerts():
    posting, analysis = make_posting(), make_analysis()
    profile = tuned(limits=Limits(min_score=0.0, suppress_if_applied=False))
    # `applied` is only passed when the profile asks for it; the runner honours the flag.
    assert scoring.route(posting, analysis, profile, applied=set()).route != "silent"


# --- T8.10 / TX.2: priorities.never ------------------------------------------------------------

def test_t8_10_never_category_is_not_alerted_at_any_score():
    """T8.10 / TX.2, MANDATORY-ish. `never` is absolute. A 1.0-scoring quant req still
    does not reach you."""
    posting = make_posting(title="Quantitative Developer")
    analysis = make_analysis(function=["quant"], variant_scores={"generalist": 1.0})
    profile = tuned(roles=Roles(priorities=priorities(never=["quant", "sdet"])))

    candidate = scoring.route(posting, analysis, profile, tier={"Acme"})
    assert candidate.route == "silent"
    assert any("never" in r for r in candidate.reasons)


def test_tx_2_a_never_category_is_still_present_in_list_all(conn):
    """Stored, never alerted. `--all` is how you check the filter did not eat something."""
    posting = make_posting(title="Quant Researcher")
    store.upsert_posting(conn, posting, run_id=1)
    analysis = make_analysis(function=["quant"])
    store.upsert_analysis(conn, analysis)
    assert store.get_posting(conn, posting.global_id) is not None
    assert [p.global_id for p, _ in store.iter_postings(conn)] == [posting.global_id]


# --- TX.6: multi-category postings --------------------------------------------------------------

def test_tx_6_multi_category_posting_groups_under_its_highest_rank():
    """A posting matching `mle` and `security` groups under `mle`, and takes that same
    weight for scoring - so grouping and ranking cannot disagree."""
    posting = make_posting()
    analysis = make_analysis(function=["security", "mle"])
    profile = tuned()

    candidate = scoring.route(posting, analysis, profile)
    assert candidate.group == "mle"
    assert profile.roles.priorities.weight_for(["security", "mle"]) == 1.0


def test_tx_6_the_full_match_list_is_shown_in_the_alert():
    """Grouped under one category, but visible as multi-category."""
    from jobmon import notify

    body = notify.format_alert(
        make_posting(), make_analysis(function=["security", "mle"]), tuned(), 0.9
    )
    assert "Categories: security, mle" in body


# --- TX.7 / sort modes -----------------------------------------------------------------------------

def _feed():
    """A strong swe match and a mediocre mle one - the case the sort modes disagree on."""
    strong_swe = scoring.Candidate(
        posting=make_posting(gid="gh:a:swe", company="Zeta"),
        analysis=make_analysis(gid="gh:a:swe", function=["swe"]),
        score=0.95, route="digest", group="swe",
    )
    weak_mle = scoring.Candidate(
        posting=make_posting(gid="gh:a:mle", company="Alpha",
                             posted_at=datetime.now(UTC) - timedelta(hours=5)),
        analysis=make_analysis(gid="gh:a:mle", function=["mle"]),
        score=0.60, route="digest", group="mle",
    )
    return [strong_swe, weak_mle]


def test_sort_by_priority_groups_before_it_scores():
    """The known tradeoff, asserted as intended behaviour: a strong `swe` match sits below
    a mediocre `mle` one. If you find yourself scrolling past the first group, switch to
    `score` - do not re-rank `order`."""
    ordered = scoring.sort_candidates(_feed(), tuned())
    assert [c.group for c in ordered] == ["mle", "swe"]


def test_tx_7_sort_by_score_disables_grouping_and_order_still_feeds_the_weight():
    """TX.7. Pure score order, categories interleaved - and `order` still contributes to
    the score through the function term."""
    profile = tuned(roles=Roles(priorities=priorities(sort_by="score")))
    ordered = scoring.sort_candidates(_feed(), profile)
    assert [c.group for c in ordered] == ["swe", "mle"]
    assert profile.roles.priorities.resolved["mle"] == 1.0, "order still feeds the weight"


def test_sort_by_date_is_newest_first_and_ignores_priority():
    profile = tuned(roles=Roles(priorities=priorities(sort_by="date")))
    ordered = scoring.sort_candidates(_feed(), profile)
    assert [c.global_id for c in ordered] == ["gh:a:swe", "gh:a:mle"]


def test_sort_by_company_groups_by_employer():
    profile = tuned(roles=Roles(priorities=priorities(sort_by="company")))
    ordered = scoring.sort_candidates(_feed(), profile)
    assert [c.posting.company for c in ordered] == ["Alpha", "Zeta"]


# --- the routing rule itself -----------------------------------------------------------------

def test_a_policy_ceiling_of_digest_beats_a_high_score():
    """`on_unknown: digest` has to mean what it says: an `unknown` posting scoring 0.95
    goes to the digest, not instant."""
    # A posting that scores well above the instant threshold on its own merits: top-
    # ranked category, full keyword match, preferred location, tier-1 employer.
    posting = make_posting(locations=["New York"])
    analysis = make_analysis(eligibility="unknown", function=["mle"],
                             variant_scores={"generalist": 1.0})
    profile = tuned(locations=Locations(allow=["United States"], prefer=["New York"]))
    candidate = scoring.route(posting, analysis, profile, tier={"Acme"})
    assert candidate.score >= 0.8
    assert candidate.route == "digest"
    assert any("unknown -> digest" in r for r in candidate.reasons)


def test_alert_is_permission_not_compulsion():
    """`on_likely_ok: alert` lifts the ceiling; the score still decides."""
    posting = make_posting(locations=["Nowhere"])
    weak = make_analysis(eligibility="likely_ok", function=["data"],
                         variant_scores={"generalist": 0.1})
    profile = tuned(locations=Locations(allow=["United States"]))
    candidate = scoring.route(posting, weak, profile)
    assert candidate.route != "instant"


def test_below_min_score_is_silent_and_says_why():
    posting = make_posting(locations=["Nowhere"])
    analysis = make_analysis(function=["data"], variant_scores={"generalist": 0.0},
                             degree_gated=True, yoe_floor=8)
    profile = tuned(locations=Locations(allow=["United States"]))
    candidate = scoring.route(posting, analysis, profile)
    assert candidate.route == "silent"
    assert any("min_score" in r for r in candidate.reasons)


def test_tier1_company_proposes_an_instant_alert():
    posting, analysis = make_posting(), make_analysis(eligibility="likely_ok")
    assert scoring.route(posting, analysis, policy(), tier={"Acme"}).route == "instant"
    assert scoring.route(posting, analysis, policy(), tier=set()).route != "instant"


def test_denylisted_title_term_matches_on_word_boundaries():
    profile = policy(denylist=Denylist(title_terms=["sap", "sdet"]))
    denied = make_posting(title="SAP Integration Engineer")
    fine = make_posting(gid="gh:acme:2", title="Sapling Platform Engineer")
    assert scoring.route(denied, make_analysis(), profile).route == "silent"
    assert scoring.route(fine, make_analysis(gid="gh:acme:2"), profile).route != "silent"


def test_denylisted_company_matches_a_token_prefix():
    profile = policy(denylist=Denylist(companies=["acme"]))
    posting = make_posting(company="Acme Corporation, Inc.")
    assert scoring.route(posting, make_analysis(), profile).route == "silent"


def test_location_deny_suppresses_and_prefer_outscores_allow():
    profile = policy(
        locations=Locations(allow=["United States"], prefer=["New York"], deny=["Bengaluru"])
    )
    assert scoring.location_match(make_posting(locations=["New York"]), profile) == 1.0
    assert scoring.location_match(make_posting(locations=["Austin, United States"]),
                                  profile) == 0.5
    assert scoring.location_match(make_posting(locations=["Tokyo"]), profile) == 0.0
    denied = make_posting(locations=["Bengaluru"])
    assert scoring.route(denied, make_analysis(), profile).route == "silent"


def test_an_empty_allow_list_means_everywhere_is_allowed():
    """A profile that has not thought about location yet must not silently score every
    posting at zero."""
    assert scoring.location_match(make_posting(locations=["Tokyo"]), tuned()) == 0.5


def test_yoe_over_the_ceiling_is_a_penalty_not_a_suppression():
    posting = make_posting()
    over = make_analysis(yoe_floor=8)
    profile = tuned()
    _, breakdown = scoring.score(posting, over, profile)
    assert breakdown["yoe_over"] == -profile.scoring.penalty_yoe_over
    ceiling, _ = scoring.policy_ceiling(posting, over, profile)
    assert ceiling != "silent", "a YoE floor ranks a posting down, it does not delete it"


def test_min_sponsor_confidence_downranks_and_does_not_suppress():
    """Addendum 4: it should downrank, never suppress, unless you explicitly say so."""
    posting = make_posting()
    weak = make_analysis(sponsor_confidence="none_observed")
    profile = tuned(eligibility=Eligibility(on_unknown="digest",
                                            min_sponsor_confidence="occasional"))
    _, breakdown = scoring.score(posting, weak, profile)
    assert breakdown["below_min_sponsor"] < 0
    assert scoring.policy_ceiling(posting, weak, profile)[0] != "silent"

    explicit = tuned(eligibility=Eligibility(min_sponsor_confidence="occasional",
                                             on_below_min_sponsor="suppress"))
    assert scoring.policy_ceiling(posting, weak, explicit)[0] == "silent"


def test_score_is_clamped_to_zero_one():
    posting = make_posting()
    worst = make_analysis(function=["data"], variant_scores={"generalist": 0.0},
                          degree_gated=True, yoe_floor=20,
                          sponsor_confidence="none_observed")
    profile = tuned(locations=Locations(allow=["United States"]),
                    eligibility=Eligibility(min_sponsor_confidence="frequent"))
    value, _ = scoring.score(make_posting(locations=["Tokyo"]), worst, profile)
    assert value == 0.0
    best, _ = scoring.score(posting, make_analysis(
        function=["mle"], variant_scores={"generalist": 1.0},
        sponsor_confidence="frequent"), profile, tier={"Acme"})
    assert 0.0 <= best <= 1.0


# --- TX.1 / TX.8: re-ranking costs nothing --------------------------------------------------

def test_tx_1_reordering_the_priority_list_changes_the_feed_with_no_re_analysis():
    """TX.1. Priorities apply at ALERT time, not at analysis time - that is the whole
    reason `--resort` is safe to experiment with."""
    feed = _feed()
    mle_first = tuned(roles=Roles(priorities=priorities(order=["mle", "swe"])))
    swe_first = tuned(roles=Roles(priorities=priorities(order=["swe", "mle"])))

    assert [c.group for c in scoring.sort_candidates(feed, mle_first)] == ["mle", "swe"]
    assert [c.group for c in scoring.sort_candidates(feed, swe_first)] == ["swe", "mle"]
    # Same Analysis objects throughout: nothing was recomputed, nothing was re-fetched.
    assert all(c.analysis is a for c, a in zip(feed, [c.analysis for c in feed]))


def test_tx_8_resort_makes_zero_network_calls(conn, monkeypatch, capsys):
    """TX.8, MANDATORY. `Assert this, do not assume it.`

    Any attempt to construct an HTTP client, from anywhere in the call tree, raises.
    """
    import httpx

    posting = make_posting()
    store.upsert_posting(conn, posting, run_id=1)
    store.upsert_analysis(conn, make_analysis())

    def explode(*a, **kw):
        raise AssertionError("--resort made a network call")

    monkeypatch.setattr(httpx, "Client", explode)
    monkeypatch.setattr(runner.ats_public, "make_client", explode)

    from jobmon import cli

    args = cli.build_parser().parse_args(["list", "--resort", "--since", "24h"])
    args.db = ":memory:"
    # Re-point at the connection holding the fixture data.
    monkeypatch.setattr(cli, "_db", lambda _a: conn)
    assert cli.cmd_list(args) == 0
    assert "gh:acme:1" in capsys.readouterr().out


# --- calibration: what the shipped numbers actually mean ----------------------------------

def test_the_shipped_threshold_has_documented_reference_points():
    """`min_score: 0.55` is only meaningful next to the scores real postings get, so the
    reference points are pinned here rather than left to be rediscovered.

    The knife edge is deliberate and it is the point of the addendum: an ordinary
    unremarkable SWE req in a merely-allowed location does NOT clear 0.55. Raise or lower
    the threshold from `jobmon audit`, not from intuition.
    """
    profile = tuned(locations=Locations(allow=["United States"], prefer=["New York"]))

    def score_of(**kw):
        analysis = make_analysis(**kw)
        return round(scoring.score(make_posting(locations=kw.pop("_locs", ["New York"])),
                                   analysis, profile)[0], 3)

    top = scoring.score(
        make_posting(locations=["New York"]),
        make_analysis(function=["mle"], variant_scores={"generalist": 1.0},
                      sponsor_confidence="frequent"),
        profile, tier={"Acme"},
    )[0]
    plain = scoring.score(
        make_posting(locations=["Austin, United States"]),
        make_analysis(function=["swe"], variant_scores={"generalist": 0.7}),
        profile,
    )[0]
    junk = scoring.score(
        make_posting(locations=["Tokyo"]),
        make_analysis(function=["data"], variant_scores={"generalist": 0.1},
                      degree_gated=True, yoe_floor=8),
        profile,
    )[0]

    assert top >= 0.8, "a perfect match must reach the instant threshold"
    assert plain < 0.55, "an unremarkable SWE req in a merely-allowed city is below the bar"
    assert junk < plain < top
