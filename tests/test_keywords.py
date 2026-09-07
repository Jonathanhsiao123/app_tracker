"""Testbench T5 - keywords, gap list and variant scoring (spec 8, 9; build step 5).

The gate: real gaps are distinguishable from cosmetic ones. No ML, no embeddings, no LLM
calls anywhere in this path - a curated dictionary, and that is the point (spec 8).
"""

import pytest

from jobmon import config_loader
from jobmon.analyze import keywords, variants


@pytest.fixture(scope="module")
def cfg():
    return config_loader.load()


@pytest.fixture(scope="module")
def taxonomy(cfg):
    return cfg.taxonomy


@pytest.fixture(scope="module")
def inventory(cfg):
    return cfg.skill_inventory


def gaps_for(text, taxonomy, inventory, title=""):
    return keywords.gap_list(keywords.match(text, title, taxonomy), inventory)


# --- T5.1 / T5.2: real vs cosmetic -------------------------------------------------

def test_t5_1_kubernetes_is_a_real_gap(taxonomy, inventory):
    """Nothing in the inventory's infra category, and no equivalent held."""
    gaps = gaps_for("Experience with Kubernetes required.", taxonomy, inventory)
    k = [g for g in gaps if g["term"] == "kubernetes"]
    assert k and k[0]["type"] == "real"
    assert k[0]["substitute"] == ""


def test_t5_2_jax_is_a_cosmetic_gap_naming_pytorch(taxonomy, inventory):
    """They said JAX, I have PyTorch. The substitute must be named, not hidden."""
    gaps = gaps_for("Experience with JAX and programming required.", taxonomy, inventory)
    j = [g for g in gaps if g["term"] == "jax"]
    assert j and j[0]["type"] == "cosmetic"
    assert j[0]["substitute"] == "pytorch"


def test_a_held_skill_is_not_a_gap_at_all(taxonomy, inventory):
    gaps = gaps_for("Deep experience with PyTorch.", taxonomy, inventory)
    assert not any(g["term"] == "pytorch" for g in gaps)


def test_cosmetic_requires_the_substitute_to_actually_be_held(taxonomy, inventory):
    """`gcp: aws` is in the equivalents map, but there is no aws in the inventory either,
    so a gcp gap stays REAL. An equivalents map trusted blindly gets this wrong."""
    gaps = gaps_for("Experience with GCP required.", taxonomy, inventory)
    g = [x for x in gaps if x["term"] == "gcp"]
    assert g and g[0]["type"] == "real"


def test_gap_output_visibly_distinguishes_the_two(taxonomy, inventory):
    text = "Experience with Kubernetes, JAX and Terraform. Strong programming skills."
    rendered = keywords.format_gaps(gaps_for(text, taxonomy, inventory))
    assert "kubernetes" in rendered and "terraform" in rendered
    assert "jax (~pytorch)" in rendered
    assert rendered.index("kubernetes") < rendered.index("jax")  # real gaps first


# --- T5.3 / T5.4: degree gating ----------------------------------------------------

def test_t5_3_ms_or_phd_required_is_degree_gated():
    gated, text = keywords.degree_requirement("MS or PhD required in a related field.")
    assert gated is True
    assert text == "MS or PhD required in a related field."


def test_t5_4_ms_preferred_is_not_degree_gated():
    gated, text = keywords.degree_requirement("MS preferred but not required.")
    assert gated is False
    assert text is None


def test_degree_gated_is_deprioritized_not_suppressed():
    """`should_alert` lists the four suppression rules; degree gating is not among them."""
    from datetime import UTC, datetime

    from jobmon import notify
    from jobmon.models import Analysis, Posting

    now = datetime.now(UTC)
    posting = Posting(
        global_id="g:1", source="greenhouse", company="Acme", title="MLE",
        locations=[], description_html="", description_text="", apply_url="u",
        first_seen_at=now, last_seen_at=now,
    )
    analysis = Analysis(
        global_id="g:1", role_class="new_grad", function=["mle"], eligibility="unknown",
        eligibility_evidence=[], keywords={}, gaps=[], recommended_variant="mle",
        variant_scores={}, degree_gated=True,
    )
    assert notify.should_alert(posting, analysis, set())[0] is True


# --- T5.5 / T5.6: years of experience ----------------------------------------------

def test_t5_5_five_plus_years_extracts_a_floor_of_five():
    assert keywords.yoe_floor("5+ years of experience required.") == 5


def test_t5_5_role_with_a_high_floor_is_rejected_upstream():
    """The rejection happens in the role filter, not here - keywords only reports."""
    from jobmon.filters import role

    assert role.classify_role("Software Engineer", "5+ years of experience.") == "experienced"


def test_t5_6_no_experience_required_extracts_no_floor():
    assert keywords.yoe_floor("No experience required; we train new grads.") is None


# --- T5.7 / T5.8 / T5.9: variant scoring on real-shaped text -----------------------

ADVERSARIAL_ML_POSTING = """
    Machine Learning Engineer, Trustworthy AI. You will research adversarial robustness
    of large language models, build evaluation harnesses in PyTorch, and study
    reinforcement learning from human feedback. Strong Python programming required.
"""

FIRMWARE_POSTING = """
    Embedded Software Engineer. You will write firmware and device drivers for our
    instrumentation platform, work with RTOS scheduling and DSP pipelines, and build
    operator tooling in PyQt. Experience with C programming and Python is required.
"""


def test_t5_7_adversarial_ml_posting_prefers_mle_and_ranks_systems_lowest(cfg):
    matched = keywords.match(ADVERSARIAL_ML_POSTING, "Machine Learning Engineer", cfg.taxonomy)
    scores = variants.score(matched, cfg.variants)
    assert variants.recommend(scores) in ("mle", "security")
    assert min(scores, key=lambda n: scores[n]) == "systems"


def test_t5_8_firmware_and_instrument_posting_prefers_systems(cfg):
    matched = keywords.match(FIRMWARE_POSTING, "Embedded Software Engineer", cfg.taxonomy)
    scores = variants.score(matched, cfg.variants)
    assert variants.recommend(scores) == "systems"


def test_t5_9_a_close_call_is_flagged_rather_than_asserted(cfg):
    """Top two within the margin -> not confident, so both are surfaced."""
    scores = {"mle": 0.82, "security": 0.78, "generalist": 0.30, "systems": 0.10}
    confident, reason = variants.is_confident(scores)
    assert confident is False
    assert "lead" in reason


def test_t5_10_no_output_contains_generated_prose(cfg):
    """Report-only. Nothing in this path emits resume text (spec 2)."""
    matched = keywords.match(ADVERSARIAL_ML_POSTING, "MLE", cfg.taxonomy)
    summary = keywords.summary_line(matched)
    rendered = keywords.format_gaps(keywords.gap_list(matched, cfg.skill_inventory))
    for out in (summary, rendered):
        assert "\n" not in out          # a single line, not prose
        assert len(out.split()) < 40    # a list of terms, not a sentence


# --- matching mechanics ------------------------------------------------------------

def test_aliases_resolve_to_the_canonical_term(taxonomy):
    matched = keywords.match("Experience with k8s and LLMs.", "", taxonomy)
    assert "kubernetes" in matched["infra"]
    assert "llm" in matched["ml_topics"]
    assert "k8s" not in matched["infra"]


def test_cpp_variants_collapse_to_one_entry(taxonomy):
    matched = keywords.match("Strong C++17 and CPP programming experience.", "", taxonomy)
    assert matched["languages"] == ["c++"]


def test_rl_and_reinforcement_learning_are_one_term(taxonomy):
    matched = keywords.match("Deep RL and reinforcement learning research.", "", taxonomy)
    assert matched["ml_topics"] == ["reinforcement learning"]


def test_bare_go_without_a_language_cue_does_not_match(taxonomy):
    """"Go Engine from Scratch" is a board game; "go to market" is a strategy."""
    matched = keywords.match("We go to market fast and let engineers go deep.", "", taxonomy)
    assert "go" not in matched.get("languages", [])


def test_go_with_a_language_cue_does_match(taxonomy):
    matched = keywords.match("Proficient in Go and Python programming.", "", taxonomy)
    assert "go" in matched["languages"]


def test_bare_c_in_prose_does_not_match(taxonomy):
    matched = keywords.match("Option C is the best of the three.", "", taxonomy)
    assert "c" not in matched.get("languages", [])


def test_python_needs_no_cue(taxonomy):
    assert "python" in keywords.match("We use Python here.", "", taxonomy)["languages"]


def test_title_is_searched_as_well_as_the_body(taxonomy):
    matched = keywords.match("", "Kubernetes Platform Engineer", taxonomy)
    assert "kubernetes" in matched["infra"]


def test_category_order_follows_the_yaml(taxonomy):
    text = "Python, Kubernetes, PyTorch, firmware, intrusion detection, adversarial."
    matched = keywords.match(text, "", taxonomy)
    assert list(matched) == [c for c in taxonomy if c in matched]


def test_empty_description_matches_nothing(taxonomy):
    assert keywords.match("", "", taxonomy) == {}


def test_no_gaps_when_nothing_matched(taxonomy, inventory):
    assert keywords.gap_list({}, inventory) == []


# --- summary line ------------------------------------------------------------------

def test_summary_line_is_capped(taxonomy):
    text = " ".join(t for terms in taxonomy.values() for t in terms)
    line = keywords.summary_line(keywords.match(text, "", taxonomy), max_len=60)
    assert len(line) <= 64
    assert line.endswith("...")


def test_summary_line_groups_by_category(taxonomy):
    matched = keywords.match("Python and Kubernetes.", "", taxonomy)
    assert keywords.summary_line(matched) == "python | kubernetes"


# --- the shipped inventory ---------------------------------------------------------

def test_inventory_terms_all_exist_in_the_taxonomy(cfg):
    """A typo in the inventory silently turns a held skill into a permanent gap."""
    known = {t for terms in cfg.taxonomy.values() for t in terms}
    for category, terms in cfg.skill_inventory.items():
        if category == "equivalents" or not isinstance(terms, list):
            continue
        for term in terms:
            assert term in known, f"{term!r} in skill_inventory is not in taxonomy.yaml"


def test_equivalents_keys_and_values_exist_in_the_taxonomy(cfg):
    known = {t for terms in cfg.taxonomy.values() for t in terms}
    for term, substitute in (cfg.skill_inventory.get("equivalents") or {}).items():
        assert term in known, f"equivalents key {term!r} not in taxonomy"
        assert substitute in known, f"equivalents value {substitute!r} not in taxonomy"


def test_inventory_does_not_claim_go_or_aws(cfg):
    """Regression on two real traps: "Go Engine" is the board game, and "Amazon-funded"
    is funding, not AWS. Both would be inferred skills the resume does not support."""
    assert "go" not in cfg.skill_inventory["languages"]
    assert "aws" not in cfg.skill_inventory["infra"]


# --- ambiguous-term stop phrases (found on real postings) --------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Drive global go-to-market programmes and develop joint solutions.",
        "When something breaks, you go to root cause and develop a fix.",
        "Our engineers go deep on the code they own.",
        "Ready to go live with the new programming stack.",
    ],
)
def test_go_inside_an_english_phrase_is_not_the_language(taxonomy, text):
    """Each of these has a cue word nearby and still is not Go."""
    assert "go" not in keywords.match(text, "", taxonomy).get("languages", [])


@pytest.mark.parametrize(
    "text",
    [
        "Strong backend chops in a modern language - Python, Go, Java, Rust, or similar.",
        "Stack: Python, Go, C++, PyTorch, Kubernetes.",
        "Proficient in at least one programming language (Python, TypeScript, Go).",
    ],
)
def test_go_in_a_language_list_is_the_language(taxonomy, text):
    """Real sentences from live postings; all three genuinely want Go."""
    assert "go" in keywords.match(text, "", taxonomy)["languages"]


# --- the alias layer, and what it fixed --------------------------------------------

@pytest.mark.parametrize(
    "text,canonical",
    [
        ("Trained a YOLOv8 detector.", "computer vision"),
        ("Object detection on embedded hardware.", "computer vision"),
        ("TinyML deployment on microcontrollers.", "embedded"),
        ("Bare metal firmware for an MCU.", "embedded"),
        ("Control software for CW EPR spectrometers.", "instrumentation"),
        ("Built a digital lock-in amplifier.", "instrumentation"),
        ("Kubernetes/EKS in production.", "kubernetes"),
        ("Trained a PPO agent.", "reinforcement learning"),
        ("Measured robustness under evasion attacks.", "adversarial"),
    ],
)
def test_domain_synonyms_resolve_to_the_canonical_term(taxonomy, text, canonical):
    matched = keywords.match(text, "", taxonomy)
    assert canonical in {t for terms in matched.values() for t in terms}


@pytest.mark.parametrize("term", ["computer vision", "embedded", "instrumentation"])
def test_previously_withheld_terms_are_now_held(cfg, term):
    """Each of these cost ~200-500 phantom "real gaps" while withheld as inference. They
    are now evidenced through ALIASES against the resume text, not guessed."""
    held = {t for k, v in cfg.skill_inventory.items()
            if k != "equivalents" and isinstance(v, list) for t in v}
    assert term in held


def test_cpp_is_cosmetic_against_c(taxonomy, inventory):
    """The single most common gap in the corpus (830 postings). C is a real substitute for
    a new-grad req listing "C/C++"; delete the equivalents line to make it hard again."""
    gaps = gaps_for("Strong C++ programming required.", taxonomy, inventory)
    cpp = [g for g in gaps if g["term"] == "c++"]
    assert cpp and cpp[0]["type"] == "cosmetic" and cpp[0]["substitute"] == "c"


def test_fpga_is_cosmetic_against_verilog(taxonomy, inventory):
    gaps = gaps_for("FPGA development experience.", taxonomy, inventory)
    f = [g for g in gaps if g["term"] == "fpga"]
    assert f and f[0]["type"] == "cosmetic" and f[0]["substitute"] == "verilog"


@pytest.mark.parametrize("term", ["typescript", "kubernetes", "aws", "docker", "rust", "go"])
def test_genuinely_absent_skills_stay_real_gaps(taxonomy, inventory, term):
    """The equivalents map must not launder a real gap into a cosmetic one."""
    gaps = gaps_for(f"Experience with {term} programming required.", taxonomy, inventory)
    hit = [g for g in gaps if g["term"] == term]
    assert hit and hit[0]["type"] == "real"


def test_go_engine_is_the_board_game_not_the_language(taxonomy):
    """From the resume itself: "Go Engine from Scratch, AlphaGo-Style MCTS"."""
    text = "Go Engine from Scratch, AlphaGo-Style MCTS. Python, PyTorch. Strong skills."
    assert "go" not in keywords.match(text, "", taxonomy).get("languages", [])


# --- performance: the combined pattern is built once, not per call -----------------

def test_the_compiled_taxonomy_is_cached(taxonomy):
    """Rebuilding ~100 patterns per posting cost 10.6ms each - 144s over the corpus."""
    keywords._compiled.cache_clear()
    for _ in range(50):
        keywords.match("Python and Kubernetes.", "", taxonomy)
    info = keywords._compiled.cache_info()
    assert info.misses == 1
    assert info.hits == 49


def test_match_is_a_single_pass_over_the_document(taxonomy):
    """One combined alternation, so a long document is scanned once regardless of how
    many taxonomy terms exist."""
    combined, _, _, _ = keywords._compiled(keywords._taxonomy_key(taxonomy))
    assert combined.groups == 1
