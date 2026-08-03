"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .cache import DetailCache
from .client import FKEClient
from .config import (
    base_url_from,
    effective_concurrency,
    load_config,
    load_credentials,
    HARD_CONCURRENCY_CAP,
)
from .dates import (
    DEFAULT_SEARCH_FORMAT,
    SEARCH_FORMATS,
    detect_ordering,
    format_uk,
    has_numeric_dates,
    parse_display_date,
    parse_user_date,
)
from .errors import DataError, FKEError
from .parse import ListingRow, parse_detail, parse_listing
from .report import build_bookings, load_invoiced_refs, write_details, write_invoice
from .themes import ThemeMapper

DEFAULT_SEARCH_START = date(2000, 1, 1)


def log(message: str = "") -> None:
    print(message, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fke-pull",
        description=(
            "Pull booking data out of the Fun Kids Entertainers portal and write "
            "invoice.csv and details.csv."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Credentials come from FKE_USER / FKE_PASS in the environment or in\n"
            ".env (which is gitignored).\n\n"
            "Examples:\n"
            "  fke-pull\n"
            "  fke-pull --since 01/04/2026\n"
            "  fke-pull --since 01/04/2026 --invoiced-refs invoiced.txt\n"
            "  fke-pull --source both --invoiced-mode mark\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"fke-pull {__version__}")

    parser.add_argument(
        "--since", metavar="DATE",
        help="only include parties on or after this date (DD/MM/YYYY or YYYY-MM-DD)",
    )
    parser.add_argument(
        "--until", metavar="DATE",
        help="only include parties on or before this date (DD/MM/YYYY or YYYY-MM-DD)",
    )
    parser.add_argument(
        "--source", choices=("completed", "upcoming", "both"), default="completed",
        help="which listing to pull (default: completed — the parties you can invoice for)",
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


def _search_completed(
    client: FKEClient, cfg: dict, start: date, end: date, search_format: str
) -> list[ListingRow]:
    html, url = client.fetch_completed_html(
        cfg["site"]["completed_path"], start, end, search_format
    )
    return parse_listing(html, url=url, source="completed")


def _try_search(client: FKEClient, cfg: dict, start: date, end: date, fmt: str) -> list[ListingRow]:
    """A search that treats any failure as "this format found nothing"."""
    try:
        return _search_completed(client, cfg, start, end, fmt)
    except FKEError:
        return []


def _fetch_completed(
    client: FKEClient, cfg: dict, cache: DetailCache, start: date, end: date
) -> tuple[list[ListingRow], str]:
    """Run the booking history search, working out which date order it wants.

    Sending DateRange in the wrong order doesn't error — it just returns fewer
    bookings, which would quietly drop lines off an invoice. So on the first run
    we send the same search both ways and keep whichever finds more, remember
    the answer, and re-check any time a search comes back empty.
    """
    configured = str(cfg["site"].get("search_date_format", "auto")).upper()
    if configured in SEARCH_FORMATS:
        rows = _search_completed(client, cfg, start, end, configured)
        if rows:
            return rows, configured
        other = _other_format(configured)
        alt = _try_search(client, cfg, start, end, other)
        if alt:
            log(
                f"WARNING: the search found nothing sent as {configured}, but "
                f"{len(alt)} booking(s) when sent as {other}. Using {other} — "
                f"change search_date_format in config.toml to match."
            )
            return alt, other
        return rows, configured

    remembered = cache.remembered_search_format()
    if remembered:
        rows = _search_completed(client, cfg, start, end, remembered)
        if rows:
            return rows, remembered
        other = _other_format(remembered)
        alt = _try_search(client, cfg, start, end, other)
        if alt:
            log(f"  the DateRange field now wants {other}, not {remembered} — relearning")
            cache.remember_search_format(other)
            return alt, other
        return rows, remembered

    log("  checking which date order the search field wants…")
    results = {fmt: _try_search(client, cfg, start, end, fmt) for fmt in SEARCH_FORMATS}
    best = max(SEARCH_FORMATS, key=lambda fmt: len(results[fmt]))
    counts = ", ".join(f"{fmt}: {len(results[fmt])}" for fmt in SEARCH_FORMATS)
    if all(not rows for rows in results.values()):
        log(f"  no bookings either way ({counts}); assuming {DEFAULT_SEARCH_FORMAT}")
        return [], DEFAULT_SEARCH_FORMAT
    log(f"  DateRange wants {best} ({counts})")
    cache.remember_search_format(best)
    return results[best], best


def _other_format(fmt: str) -> str:
    return "DMY" if fmt == "MDY" else "MDY"


def _collect_listings(
    client: FKEClient, cfg: dict, args, cache: DetailCache, start: date, end: date
) -> tuple[list[ListingRow], str]:
    rows: list[ListingRow] = []
    search_format = DEFAULT_SEARCH_FORMAT
    if args.source in ("completed", "both"):
        log("Fetching completed bookings…")
        completed, search_format = _fetch_completed(client, cfg, cache, start, end)
        log(f"  {len(completed)} row(s)")
        rows.extend(completed)
    if args.source in ("upcoming", "both"):
        log("Fetching upcoming bookings…")
        html, url = client.fetch_upcoming_html(cfg["site"]["upcoming_path"])
        upcoming = parse_listing(html, url=url, source="upcoming")
        log(f"  {len(upcoming)} row(s)")
        rows.extend(upcoming)
    return rows, search_format


def _dedupe(rows: list[ListingRow]) -> tuple[list[ListingRow], int]:
    seen: dict[str, ListingRow] = {}
    duplicates = 0
    for row in rows:
        if row.ref in seen:
            duplicates += 1
            continue
        seen[row.ref] = row
    return list(seen.values()), duplicates


def _resolve_ordering(cfg: dict, rows: list[ListingRow], more_samples=None) -> str:
    """Decide whether the portal writes 14/06/2026 or 06/14/2026.

    Needs at least one date with a day above the 12th to be sure. If this batch
    hasn't got one, we ask the portal for a wide date range purely to get more
    dates to look at — one extra request beats guessing.
    """
    configured = str(cfg["site"]["display_date_format"]).upper()
    samples = [row.event_date_raw for row in rows]
    if configured in ("DMY", "MDY"):
        return configured

    detected = detect_ordering(samples)
    if detected:
        return detected

    if has_numeric_dates(samples) and more_samples is not None:
        extra = more_samples()
        if extra:
            detected = detect_ordering(samples + extra)
            if detected:
                return detected

    if has_numeric_dates(samples):
        raise DataError(
            "Every date in this batch could be read either as day/month or "
            "month/day (no day above the 12th), so there is no way to tell which "
            "the portal means. Reading them the wrong way round would put the "
            "wrong dates on your invoice, so I've stopped.\n"
            "Fix: set display_date_format = \"DMY\" (or \"MDY\") under [site] in "
            "config.toml, or widen --since so the batch includes a date after the "
            "12th of a month."
        )
    return "DMY"  # all dates were written out in words; ordering is irrelevant


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    cfg = load_config(args.config if Path(args.config).exists() or args.config != "config.toml" else None)
    themes = ThemeMapper.from_config(cfg)
    username, password = load_credentials(args.env_file)
    base_url = base_url_from(cfg, args.env_file, args.base_url)
    concurrency = effective_concurrency(cfg, args.max_concurrency)

    since = parse_user_date(args.since) if args.since else None
    until = parse_user_date(args.until) if args.until else None
    if since and until and until < since:
        raise DataError(f"--until {args.until} is before --since {args.since}")

    invoiced_refs = load_invoiced_refs(args.invoiced_refs) if args.invoiced_refs else set()

    cache = DetailCache(
        directory=Path(args.cache_dir or cfg["cache"]["dir"]),
        enabled=bool(cfg["cache"]["enabled"]) and not args.no_cache,
        future_ttl_hours=float(cfg["cache"]["future_ttl_hours"]),
        refresh=args.refresh_cache,
    )

    client = FKEClient(
        base_url=base_url,
        username=username,
        password=password,
        timeout=int(cfg["fetch"]["timeout"]),
        retries=int(cfg["fetch"]["retries"]),
        max_concurrency=concurrency,
        request_delay=float(cfg["fetch"]["request_delay"]),
        user_agent=str(cfg["fetch"]["user_agent"]),
        login_path=str(cfg["site"]["login_path"]),
        username_field=cfg["site"].get("username_field"),
        password_field=cfg["site"].get("password_field"),
        verbose=args.verbose,
        log=log,
    )

    log(f"Logging in to {base_url} as {username}…")
    client.login()

    search_start = since or DEFAULT_SEARCH_START
    search_end = until or date(date.today().year + 2, 12, 31)
    rows, search_format = _collect_listings(
        client, cfg, args, cache, search_start, search_end
    )
    if not rows:
        log("No bookings returned. Nothing to write.")
        return 0

    rows, duplicates = _dedupe(rows)
    if duplicates:
        log(f"  {duplicates} booking(s) appeared in both listings; kept once")

    def wider_date_samples() -> list[str]:
        log("  dates in this batch are ambiguous; fetching a wider range to check…")
        wide = _try_search(
            client, cfg, DEFAULT_SEARCH_START, date(date.today().year + 2, 12, 31), search_format
        )
        return [row.event_date_raw for row in wide]

    ordering = _resolve_ordering(cfg, rows, wider_date_samples)
    if args.verbose:
        log(f"  reading portal dates as {ordering}")

    # Filter locally as well as in the search, because the server-side filter is
    # someone else's code and this one we can see.
    kept: list[ListingRow] = []
    for row in rows:
        event_date = parse_display_date(row.event_date_raw, ordering)
        if since and event_date < since:
            continue
        if until and event_date > until:
            continue
        kept.append(row)
    dropped = len(rows) - len(kept)
    if dropped:
        log(f"  {dropped} booking(s) outside the requested date range")
    rows = kept

    already_invoiced = [row for row in rows if row.ref in invoiced_refs]
    if invoiced_refs:
        log(f"  {len(already_invoiced)} of these are already invoiced ({args.invoiced_mode})")
        if args.invoiced_mode == "exclude":
            rows = [row for row in rows if row.ref not in invoiced_refs]
            for row in already_invoiced:
                log(f"    skipped {row.ref} ({row.event_date_raw})")
        unseen = invoiced_refs - {row.ref for row in rows} - {row.ref for row in already_invoiced}
        if unseen:
            log(
                f"  note: {len(unseen)} ref(s) in {args.invoiced_refs} weren't in "
                f"this batch: {', '.join(sorted(unseen))}"
            )

    if not rows:
        log("Nothing left after filtering. Nothing to write.")
        return 0

    event_dates = {row.ref: parse_display_date(row.event_date_raw, ordering) for row in rows}
    log(f"Fetching {len(rows)} detail page(s), {concurrency} at a time…")
    details = client.fetch_details(
        [row.ref for row in rows],
        detail_path=cfg["site"]["detail_path"],
        offset=int(cfg["site"]["detail_id_offset"]),
        cache=cache,
        event_dates=event_dates,
    )
    log(f"  {cache.summary()}")

    parsed_details = {
        ref: parse_detail(html, url=url) for ref, (html, url) in details.items()
    }

    result = build_bookings(
        rows,
        details,
        parsed_details,
        ordering=ordering,
        themes=themes,
        invoiced_refs=invoiced_refs,
        allow_blank_fees=args.allow_blank_fees,
    )

    out_dir = Path(args.out_dir)
    invoice_path = out_dir / args.invoice_csv
    details_path = out_dir / args.details_csv
    percent = int(cfg["invoice"]["percent"])

    write_invoice(
        invoice_path,
        result.bookings,
        percent=percent,
        rounding=str(cfg["invoice"]["rounding"]),
        total_from_rounded_lines=bool(cfg["invoice"]["total_from_rounded_lines"]),
        mark_invoiced=args.invoiced_mode == "mark" and bool(invoiced_refs),
    )
    write_details(details_path, result.bookings)

    for warning in result.warnings:
        log(f"WARNING: {warning}")
    if themes.unknown:
        log("")
        log("WARNING: no theme mapping for these package names:")
        for package in sorted(themes.unknown):
            log(f"  {package!r}")
        log("  Add them under [themes.exact] in config.toml. Until then the full")
        log("  package name (lower cased) is used as the theme.")

    first = result.bookings[0].event_date
    last = result.bookings[-1].event_date
    log("")
    log(
        f"Wrote {invoice_path} and {details_path}: {len(result.bookings)} booking(s), "
        f"{format_uk(first)} to {format_uk(last)}."
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
