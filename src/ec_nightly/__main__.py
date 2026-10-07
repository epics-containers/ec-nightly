"""Interface for ``python -m ec_nightly``."""

from __future__ import annotations

import logging
import sys
from argparse import ArgumentParser
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from . import __version__
from .checks import CHECKS, FAIL, Context, run_check
from .config import Config
from .report import write_reports

__all__ = ["main"]

log = logging.getLogger("ec_nightly")


def run_checks(
    names: Sequence[str],
    config: Config,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Run the named checks (all when empty) in order; 0 if none failed."""
    started = datetime.now(UTC)
    selected = list(names) or list(CHECKS)
    log.info(
        "ec-nightly %s: %d check(s) against %s (instrument=%s job=%s auth=%s)",
        __version__,
        len(selected),
        config.blueapi_url,
        config.instrument or "-",
        config.job_name,
        "client_credentials" if config.auth_enabled else "none",
    )
    with httpx.Client(timeout=config.timeout, transport=transport) as client:
        ctx = Context(config, client)
        results = [run_check(name, ctx) for name in selected]

    failed = [r.name for r in results if r.status == FAIL]
    if config.report_dir:
        try:
            out = write_reports(config, results, started, __version__)
            log.info("reports written to %s", out)
        except OSError as error:
            log.error("could not write reports to %s: %s", config.report_dir, error)
            return 1
    if failed:
        log.error("FAILED: %d of %d: %s", len(failed), len(results), ", ".join(failed))
        return 1
    log.info("PASSED: %d check(s)", len(results))
    return 0


def main(args: Sequence[str] | None = None) -> None:
    """Argument parser for the CLI."""
    parser = ArgumentParser(
        prog="ec-nightly",
        description="Read-only smoke checks against a blueapi instance. "
        "Configured by environment variables, see README.",
    )
    parser.add_argument("-v", "--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    run = subparsers.add_parser("run", help="run checks (default: all, in order)")
    run.add_argument(
        "checks",
        nargs="*",
        metavar="CHECK",
        help=f"one or more of: {', '.join(CHECKS)}",
    )
    subparsers.add_parser("list", help="list the available checks")
    parsed = parser.parse_args(args)

    if parsed.command == "list":
        for name, check in CHECKS.items():
            print(f"{name:12} {(check.__doc__ or '').strip()}")
    elif parsed.command == "run":
        unknown = [name for name in parsed.checks if name not in CHECKS]
        if unknown:
            parser.error(f"unknown check(s) {unknown}, choose from {list(CHECKS)}")
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-5s %(message)s",
            stream=sys.stdout,
        )
        # httpx logs request lines only, but keep its chatter out of the report
        logging.getLogger("httpx").setLevel(logging.WARNING)
        try:
            config = Config.from_env()
        except ValueError as error:
            log.error("configuration error: %s", error)
            sys.exit(2)
        sys.exit(run_checks(parsed.checks, config))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
