"""CLI entry point (spec 3, 11). CLI output plus push notifications is the whole interface -
there is no dashboard and there will not be one (spec 2).
"""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    """Wire up the subcommands.

    TODO: keep this flat, `argparse` only, no click/typer. Subcommands:

      init-db                                   create the SQLite file
      run [--dry-run] [--adapter NAME]          one pipeline pass
      list [--since 24h] [--show-blocked]       stored postings, newest first
          [--company X] [--function mle]
      show <global_id>                          full record, analysis, evidence sentences
      apply <global_id> --variant mle [--note]  mark applied (spec 11)
      log <global_id> --status screen|reject|onsite|offer [--note]
      funnel                                    THE report: conversion by variant/function
      health                                    per-adapter last-run counts and failures
      reanalyze [--all]                         re-run the analyzer after a config edit
    """
    raise NotImplementedError


def cmd_run(args: argparse.Namespace) -> int:
    """`jobmon run`. TODO: print the RunResult summary and every adapter health line -
    this is what the GitHub Actions log shows, and it is the only place silent breakage
    becomes visible without opening the DB."""
    raise NotImplementedError


def cmd_list(args: argparse.Namespace) -> int:
    """`jobmon list`. TODO: one posting per line - company, title, location, eligibility,
    variant, grad window. `--show-blocked` includes the suppressed ones."""
    raise NotImplementedError


def cmd_show(args: argparse.Namespace) -> int:
    """`jobmon show <global_id>`. TODO: print the eligibility evidence sentences verbatim
    and in full. This command exists so a `blocked` label can be overruled by eye."""
    raise NotImplementedError


def cmd_apply(args: argparse.Namespace) -> int:
    """`jobmon apply <global_id> --variant mle`. Records date and variant.
    Note: this marks that YOU applied. The system never applies on your behalf (spec 1)."""
    raise NotImplementedError


def cmd_log(args: argparse.Namespace) -> int:
    """`jobmon log <global_id> --status screen|reject|onsite|offer`."""
    raise NotImplementedError


def cmd_funnel(args: argparse.Namespace) -> int:
    """`jobmon funnel`. Applications sent, callbacks, conversion by variant and function.
    TODO: this is the number that decides whether the volume strategy is working."""
    raise NotImplementedError


def cmd_health(args: argparse.Namespace) -> int:
    """`jobmon health`. TODO: highlight adapters returning zero results without error."""
    raise NotImplementedError


def parse_since(value: str) -> object:
    """Parse "24h" / "3d" / "2w" into a datetime. TODO: UTC-aware."""
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    """Entry point. TODO: dispatch to the cmd_* functions; return the exit code.
    A failed run should exit non-zero so the Actions cron surfaces it."""
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
