"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import HARD_CONCURRENCY_CAP, load_config
from .dates import format_uk, parse_user_date
from .errors import DataError, FKEError
from .pipeline import PullOptions, build_context, pull
from .report import load_invoiced_refs, write_details, write_invoice

DEFAULT_PORT = 8765


def log(message: str = "") -> None:
    print(message, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fke-pull",
        description=(
            "Pull booking data out of the Fun Kids Entertainers portal. Writes "
            "invoice.csv and details.csv, or with --serve opens it in a browser."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Credentials come from FKE_USER / FKE_PASS in the environment or in\n"
            ".env (which is gitignored).\n\n"
            "Examples:\n"
            "  fke-pull                                  write the two CSVs\n"
            "  fke-pull --serve                          open it in a browser\n"
            "  fke-pull --since 01/04/2026\n"
            "  fke-pull --since 01/04/2026 --invoiced-refs invoiced.txt\n"
            "  fke-pull --source both --invoiced-mode mark\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"fke-pull {__version__}")

    parser.add_argument(
        "--serve", action="store_true",
        help="run the web app instead of writing CSVs: past and upcoming bookings "
             "in a browser, served on this machine only",
    )
    parser.add_argument(
        "--port", type=int, default=None, metavar="N",
        help=f"port for --serve (default: app.port in config.toml, or {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="with --serve, don't open a browser window automatically",
    )

    parser.add_argument(
        "--since", metavar="DATE",
        help="only include parties on or after this date (DD/MM/YYYY or YYYY-MM-DD)",
    )
    parser.add_argument(
        "--until", metavar="DATE",
        help="only include parties on or before this date (DD/MM/YYYY or YYYY-MM-DD)",
    )
    parser.add_argument(
        "--source", choices=("completed", "upcoming", "both"), default=None,
        help="which listing to pull (default: completed for CSVs, both for --serve)",
    )

    parser.add_argument(
        "--invoiced-refs", metavar="FILE",
        help="text file of booking refs already invoiced, one per line (# comments allowed)",
    )
    parser.add_argument(
        "--invoiced-mode", choices=("exclude", "mark"), default="exclude",
        help=(
            "exclude: leave already-invoiced bookings out of invoice.csv (default). "
            "mark: keep them, flag them in an extra column, and total both ways."
        ),
    )
    parser.add_argument(
        "--allow-blank-fees", action="store_true",
        help="don't stop when a booking's Balance is blank; list it with an empty fee",
    )

    parser.add_argument("--out-dir", default=".", metavar="DIR", help="where to write the CSVs (default: .)")
    parser.add_argument("--invoice-csv", default="invoice.csv", metavar="NAME")
    parser.add_argument("--details-csv", default="details.csv", metavar="NAME")

    parser.add_argument("--config", default="config.toml", metavar="FILE")
    parser.add_argument("--env-file", default=".env", metavar="FILE")
    parser.add_argument("--base-url", metavar="URL", help="override the portal base URL")

    parser.add_argument("--cache-dir", metavar="DIR", help="override cache location")
    parser.add_argument("--no-cache", action="store_true", help="ignore the cache and don't write to it")
    parser.add_argument("--refresh-cache", action="store_true", help="re-fetch every detail page and update the cache")

    parser.add_argument(
        "--max-concurrency", type=int, metavar="N",
        help=f"parallel requests, never more than {HARD_CONCURRENCY_CAP} (default: 5)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="show every request")
    return parser


def load_cfg(args) -> dict:
    """config.toml if it's there; the built-in defaults if it isn't."""
    explicit = args.config != "config.toml"
    return load_config(args.config if explicit or Path(args.config).exists() else None)


def options_from(args) -> PullOptions:
    since = parse_user_date(args.since) if args.since else None
    until = parse_user_date(args.until) if args.until else None
    if since and until and until < since:
        raise DataError(f"--until {args.until} is before --since {args.since}")
    return PullOptions(
        since=since,
        until=until,
        source=args.source or ("both" if args.serve else "completed"),
        invoiced_refs=load_invoiced_refs(args.invoiced_refs) if args.invoiced_refs else set(),
        invoiced_mode=args.invoiced_mode,
        allow_blank_fees=args.allow_blank_fees,
    )


def context_from(args, cfg: dict, log_fn=log):
    return build_context(
        cfg,
        env_file=args.env_file,
        base_url_override=args.base_url,
        cache_dir=args.cache_dir,
        no_cache=args.no_cache,
        refresh_cache=args.refresh_cache,
        max_concurrency=args.max_concurrency,
        verbose=args.verbose,
        log=log_fn,
    )


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_cfg(args)
    options = options_from(args)

    if args.serve:
        from .server import serve  # only imported when it's actually wanted

        port = args.port if args.port is not None else int(cfg["app"]["port"])
        return serve(args, cfg, options, port=port, open_browser=not args.no_open, log=log)

    result = pull(context_from(args, cfg), options, log)

    if not result.bookings:
        log("No bookings to write.")
        return 0

    out_dir = Path(args.out_dir)
    invoice_path = out_dir / args.invoice_csv
    details_path = out_dir / args.details_csv

    write_invoice(
        invoice_path,
        result.bookings,
        percent=int(cfg["invoice"]["percent"]),
        rounding=str(cfg["invoice"]["rounding"]),
        total_from_rounded_lines=bool(cfg["invoice"]["total_from_rounded_lines"]),
        mark_invoiced=options.invoiced_mode == "mark" and bool(options.invoiced_refs),
    )
    write_details(details_path, result.bookings)

    for warning in result.warnings:
        log(f"WARNING: {warning}")
    if result.unknown_packages:
        log("")
        log("WARNING: no theme mapping for these package names:")
        for package in result.unknown_packages:
            log(f"  {package!r}")
        log("  Add them under [themes.exact] in config.toml. Until then the full")
        log("  package name (lower cased) is used as the theme.")

    log("")
    log(
        f"Wrote {invoice_path} and {details_path}: {len(result.bookings)} booking(s), "
        f"{format_uk(result.bookings[0].event_date)} to "
        f"{format_uk(result.bookings[-1].event_date)}."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except FKEError as exc:
        log("")
        log(f"ERROR: {exc}")
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        log("Interrupted.")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
