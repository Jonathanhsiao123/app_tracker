"""Profile schema, validation, and Phase X rank-to-weight.

Testbench T8.9, T8.12 and TX.3, TX.4, TX.5, TX.9.

TX.5 and T8.12 are the two the addendum says to actually write. The first is the only way
this config can be internally contradictory; the second prevents a config typo from
restoring the flood the whole layer exists to stop.
"""

import textwrap
from pathlib import Path

import pytest

import jobmon
from jobmon import profiles
from jobmon.config_loader import rank_weights
from jobmon.profiles import ProfileError

FLOOR = rank_weights.FLOOR


def write_profile(tmp_path, name, body):
    directory = tmp_path / "profiles"
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    return directory


# --- T8.9: a minimal profile loads, every unset field takes its default ---------------

def test_t8_9_minimal_four_line_profile_loads_with_defaults(tmp_path):
    """MANDATORY-ish. `Every field has a default in code, so a minimal profile is four
    lines.` If this needs more than four lines, the defaults are not doing their job."""
    directory = write_profile(tmp_path, "tiny", """
        name: tiny
        roles:
          role_class: [new_grad]
    """)
    profile = profiles.load_all(directory)["tiny"]

    assert profile.name == "tiny"
    assert profile.roles.role_class == ["new_grad"]
    # Everything else is the built-in default, which is the pre-addendum behaviour.
    assert profile.eligibility.on_unknown == "alert"
    assert profile.eligibility.on_blocked == "suppress"
    assert profile.roles.on_unclear_role == "alert"
    assert profile.limits.min_score == 0.0
    assert profile.limits.max_instant_per_day is None
    assert profile.routing.digest == "all"
    assert profile.roles.priorities.sort_by == "priority"


def test_an_empty_profiles_directory_yields_the_builtin_default(tmp_path):
    """A fresh checkout must still run, and must behave exactly as it did before."""
    empty = tmp_path / "none"
    empty.mkdir()
    loaded = profiles.load_all(empty)
    assert list(loaded) == ["default"]
    assert loaded["default"].eligibility.on_unknown == "alert"


def test_the_shipped_profile_loads():
    """The real `jobmon/config/profiles/` must parse. A profile that will not load is a
    cron run that exits 2 at 03:17 with nobody watching."""
    # NOT `profiles.PROFILES_DIR`: conftest redirects that to an empty tmp dir so no
    # other test depends on one real user's opinions. This test is the exception.
    shipped = Path(jobmon.__file__).parent / "config" / "profiles"
    loaded = profiles.load_all(shipped)
    assert "jonathan" in loaded
    jonathan = loaded["jonathan"]
    assert jonathan.eligibility.on_unknown == "digest"
    assert jonathan.roles.on_unclear_role == "digest"
    assert jonathan.limits.max_digest_items == 25
    assert jonathan.roles.priorities.order == [
        "mle", "security", "systems", "swe", "data", "hardware", "product", "business",
    ]


# --- T8.12: a corrupt profile fails loudly and NEVER falls back ------------------------

def test_t8_12_unknown_field_fails_loudly_and_names_it(tmp_path):
    """MANDATORY. `on_unkown: digest` must not load cleanly, leave `on_unknown` at its
    permissive default, and silently restore the flood."""
    directory = write_profile(tmp_path, "typo", """
        eligibility:
          on_unkown: digest
    """)
    with pytest.raises(ProfileError) as exc:
        profiles.load_all(directory)
    assert "on_unkown" in str(exc.value)
    assert "on_unknown" in str(exc.value)  # the known-fields list names the right spelling


def test_t8_12_bad_enum_value_fails_loudly_and_names_the_field(tmp_path):
    directory = write_profile(tmp_path, "bad", """
        eligibility:
          on_unknown: quiet
    """)
    with pytest.raises(ProfileError) as exc:
        profiles.load_all(directory)
    message = str(exc.value)
    assert "on_unknown" in message and "quiet" in message
    assert "alert" in message and "digest" in message and "suppress" in message


def test_t8_12_invalid_yaml_fails_loudly_with_the_file_named(tmp_path):
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / "broken.yaml").write_text("roles:\n  - [unclosed\n", encoding="utf-8")
    with pytest.raises(ProfileError) as exc:
        profiles.load_all(directory)
    assert "broken.yaml" in str(exc.value)


def test_t8_12_never_falls_back_to_alert_everything(tmp_path):
    """The failure mode this guard exists for, stated as an assertion: there is no code
    path from a broken profile to a permissive one."""
    directory = write_profile(tmp_path, "broken", "limits:\n  min_score: not-a-number\n")
    with pytest.raises(ProfileError):
        profiles.load_all(directory)
    with pytest.raises(ProfileError):
        profiles.select(directory)


def test_unknown_top_level_block_is_named(tmp_path):
    directory = write_profile(tmp_path, "x", "limits2:\n  min_score: 0.5\n")
    with pytest.raises(ProfileError, match="limits2"):
        profiles.load_all(directory)


def test_unknown_profile_name_lists_what_exists(tmp_path):
    directory = write_profile(tmp_path, "real", "name: real\n")
    with pytest.raises(ProfileError, match="real"):
        profiles.select(directory, only="imaginary")


# --- Phase X: rank to weight ------------------------------------------------------------

def test_rank_weights_are_ordered_highest_first():
    weights = rank_weights(["mle", "security", "systems", "swe", "data"])
    values = [weights[c] for c in ["mle", "security", "systems", "swe", "data"]]
    assert values == sorted(values, reverse=True)
    assert weights["mle"] == 1.0
    assert all(v > FLOOR for v in values), "every listed rank sits above the floor"


def test_tx_3_category_in_neither_list_takes_the_floor_and_is_still_alertable(tmp_path):
    """TX.3. A new taxonomy category degrades quietly instead of disappearing."""
    directory = write_profile(tmp_path, "p", """
        roles:
          priorities:
            order: [mle, swe]
            never: [quant]
    """)
    pri = profiles.load_all(directory)["p"].roles.priorities
    assert pri.weight_for(["security"]) == FLOOR      # unlisted: the floor, not zero
    assert pri.weight_for(["security"]) > 0.0         # still alertable
    assert pri.weight_for(["mle"]) == 1.0
    assert pri.weight_for(["security", "mle"]) == 1.0  # highest-ranked match wins


def test_tx_4_weights_override_wins_for_named_categories_only(tmp_path):
    """TX.4. The escape hatch for spacing: keep `data` last, but far below `swe` rather
    than just below it."""
    directory = write_profile(tmp_path, "p", """
        roles:
          priorities:
            order: [mle, security, systems, swe, data]
            weights:
              data: 0.15
    """)
    pri = profiles.load_all(directory)["p"].roles.priorities
    assert pri.resolved["data"] == 0.15                      # override wins
    assert pri.resolved["mle"] == 1.0                        # rank-derived, untouched
    assert pri.resolved["swe"] == rank_weights(pri.order)["swe"]


def test_tx_5_category_in_both_order_and_never_fails_loudly(tmp_path):
    """TX.5, MANDATORY. The only way this config can be internally contradictory.

    Either resolution is defensible, which is exactly why the code must not pick one:
    silently ranking a category you told it never to alert, or silently dropping one you
    ranked first, are both failures you would not notice for weeks.
    """
    directory = write_profile(tmp_path, "p", """
        roles:
          priorities:
            order: [mle, quant, swe]
            never: [quant, sdet]
    """)
    with pytest.raises(ProfileError) as exc:
        profiles.load_all(directory)
    message = str(exc.value)
    assert "quant" in message
    assert "order" in message and "never" in message
    assert "sdet" not in message, "only the contradictory category is named"


def test_tx_9_empty_order_takes_floor_everywhere_warns_and_proceeds(tmp_path, caplog):
    """TX.9. Legal, occasionally what you want, and it silently disables the whole
    feature - so it is said out loud once per load."""
    directory = write_profile(tmp_path, "p", """
        roles:
          priorities:
            order: []
            sort_by: score
    """)
    with caplog.at_level("WARNING"):
        profile = profiles.load_all(directory)["p"]
    assert profile.roles.priorities.resolved == {}
    assert profile.roles.priorities.weight_for(["mle"]) == FLOOR
    assert profile.roles.priorities.weight_for(["anything"]) == FLOOR
    assert any("order is empty" in r.getMessage() for r in caplog.records)


def test_a_duplicated_category_in_order_fails(tmp_path):
    """Rank position would be ambiguous, and ambiguity here is silent."""
    directory = write_profile(tmp_path, "p", """
        roles:
          priorities:
            order: [mle, swe, mle]
    """)
    with pytest.raises(ProfileError, match="more than"):
        profiles.load_all(directory)


def test_a_category_only_in_never_is_not_an_error(tmp_path):
    """`never: [quant, sdet]` is the shipped config, and `sdet` is not a taxonomy category.
    An entry that matches nothing costs nothing; refusing to load over it would not."""
    directory = write_profile(tmp_path, "p", """
        roles:
          priorities:
            order: [mle]
            never: [quant, sdet]
    """)
    profile = profiles.load_all(directory)["p"]
    assert profile.roles.priorities.never == ["quant", "sdet"]


# --- confidence ordering -----------------------------------------------------------------

def test_confidence_meets_is_ordered_and_any_always_passes():
    assert profiles.confidence_meets("frequent", "occasional")
    assert profiles.confidence_meets("occasional", "occasional")
    assert not profiles.confidence_meets("none_observed", "occasional")
    assert not profiles.confidence_meets("unknown", "occasional")
    assert profiles.confidence_meets("none_observed", "any")


# --- T8.12 through `jobmon run` itself --------------------------------------------------

def test_t8_12_a_corrupt_profile_stops_the_run_before_a_single_board_is_fetched(
    tmp_path, monkeypatch, capsys
):
    """MANDATORY, at the level T8.12 actually specifies: `run fails loudly at load with the
    offending field named. Never falls back to "alert everything"`.

    The check is in `cmd_run` ahead of the sweep, so a typo costs you an exit code in two
    seconds rather than ten minutes of fetching followed by a traceback.
    """
    from jobmon import cli, runner
    from jobmon import profiles as profiles_mod

    directory = write_profile(tmp_path, "typo", "eligibility:\n  on_unkown: digest\n")
    monkeypatch.setattr(profiles_mod, "PROFILES_DIR", directory)
    monkeypatch.setenv("JOBMON_NTFY_TOPIC", "t")

    def must_not_run(*a, **kw):
        raise AssertionError("the sweep started despite a corrupt profile")

    monkeypatch.setattr(runner, "run", must_not_run)
    monkeypatch.setattr(runner, "run_scheduled", must_not_run)

    args = cli.build_parser().parse_args(["run"])
    assert cli.cmd_run(args) == cli.EXIT_MISCONFIGURED
    err = capsys.readouterr().err
    assert "profile config error" in err
    assert "on_unkown" in err


def test_a_good_profile_reaches_the_sweep(tmp_path, monkeypatch):
    """The corollary: the guard above rejects typos, not profiles."""
    from jobmon import cli, runner
    from jobmon import profiles as profiles_mod

    directory = write_profile(tmp_path, "fine", "eligibility:\n  on_unknown: digest\n")
    monkeypatch.setattr(profiles_mod, "PROFILES_DIR", directory)
    monkeypatch.setenv("JOBMON_NTFY_TOPIC", "t")

    seen = {}

    def fake_run(conn, **kw):
        seen["profiles"] = [p.name for p in kw["profile_list"]]
        return runner.RunResult()

    monkeypatch.setattr(runner, "run", fake_run)
    args = cli.build_parser().parse_args(["--db", ":memory:", "run"])
    assert cli.cmd_run(args) == cli.EXIT_OK
    assert seen["profiles"] == ["fine"]
