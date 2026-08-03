"""The pull itself: log in, fetch, parse, cross-check, assemble bookings.

Shared by the command line and the web app so both see identical numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from .cache import DetailCache
from .client import FKEClient
from .config import base_url_from, effective_concurrency, load_credentials
from .dates import (
    DEFAULT_SEARCH_FORMAT,
    SEARCH_FORMATS,
    detect_ordering,
    has_numeric_dates,
    parse_display_date,
)
from .errors import DataError, FKEError
from .parse import ListingRow, parse_detail, parse_listing
from .report import Booking, build_bookings
from .themes import ThemeMapper

DEFAULT_SEARCH_START = date(2000, 1, 1)

Log = Callable[[str], None]


@dataclass
class PullOptions:
    since: date | None = None
    until: date | None = None
    source: str = "completed"
    invoiced_refs: set[str] = field(default_factory=set)
    invoiced_mode: str = "exclude"
    allow_blank_fees: bool = False


@dataclass
class PullResult:
    bookings: list[Booking]
    warnings: list[str] = field(default_factory=list)
    unknown_packages: list[str] = field(default_factory=list)
    skipped_invoiced: list[str] = field(default_factory=list)
    missing_refs: list[str] = field(default_factory=list)
    search_format: str = DEFAULT_SEARCH_FORMAT
    ordering: str = "DMY"
    cache_summary: str = ""

    @property
    def past(self) -> list[Booking]:
        return [b for b in self.bookings if b.source == "completed"]

    @property
    def upcoming(self) -> list[Booking]:
        return [b for b in self.bookings if b.source != "completed"]


@dataclass
class Context:
    """Everything a pull needs, built once and reusable across refreshes."""

    cfg: dict
    client: FKEClient
    cache: DetailCache
    themes: ThemeMapper
    concurrency: int


def build_context(
    cfg: dict,
    *,
    env_file: str | Path,
    base_url_override: str | None = None,
    cache_dir: str | None = None,
    no_cache: bool = False,
    refresh_cache: bool = False,
    max_concurrency: int | None = None,
    verbose: bool = False,
    log: Log = print,
) -> Context:
    username, password = load_credentials(env_file)
    base_url = base_url_from(cfg, env_file, base_url_override)
    concurrency = effective_concurrency(cfg, max_concurrency)

    cache = DetailCache(
        directory=Path(cache_dir or cfg["cache"]["dir"]),
        enabled=bool(cfg["cache"]["enabled"]) and not no_cache,
        future_ttl_hours=float(cfg["cache"]["future_ttl_hours"]),
        refresh=refresh_cache,
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
        verbose=verbose,
        log=log,
    )
    return Context(
        cfg=cfg,
        client=client,
        cache=cache,
        themes=ThemeMapper.from_config(cfg),
        concurrency=concurrency,
    )


# ---------------------------------------------------------------------------
# Which order the DateRange search field wants
# ---------------------------------------------------------------------------


def _other_format(fmt: str) -> str:
    return "DMY" if fmt == "MDY" else "MDY"


def _search_completed(ctx: Context, start: date, end: date, fmt: str) -> list[ListingRow]:
    html, url = ctx.client.fetch_completed_html(
        ctx.cfg["site"]["completed_path"], start, end, fmt
    )
    return parse_listing(html, url=url, source="completed")


def _try_search(ctx: Context, start: date, end: date, fmt: str) -> list[ListingRow]:
    """A search that treats any failure as "this format found nothing"."""
    try:
        return _search_completed(ctx, start, end, fmt)
    except FKEError:
        return []


def fetch_completed(ctx: Context, start: date, end: date, log: Log) -> tuple[list[ListingRow], str]:
    """Run the booking history search, working out which date order it wants.

    Sending DateRange the wrong way round doesn't error — it just returns fewer
    bookings, which would quietly drop lines off an invoice. So on the first run
    we send the same search both ways and keep whichever finds more, remember
    the answer, and re-check any time a search comes back empty.
    """
    configured = str(ctx.cfg["site"].get("search_date_format", "auto")).upper()

    if configured in SEARCH_FORMATS:
        rows = _search_completed(ctx, start, end, configured)
        if rows:
            return rows, configured
        other = _other_format(configured)
        alt = _try_search(ctx, start, end, other)
        if alt:
            log(
                f"WARNING: the search found nothing sent as {configured}, but "
                f"{len(alt)} booking(s) when sent as {other}. Using {other} — "
                "change search_date_format in config.toml to match."
            )
            return alt, other
        return rows, configured

    remembered = ctx.cache.remembered_search_format()
    if remembered:
        rows = _search_completed(ctx, start, end, remembered)
        if rows:
            return rows, remembered
        other = _other_format(remembered)
        alt = _try_search(ctx, start, end, other)
        if alt:
            log(f"  the DateRange field now wants {other}, not {remembered} — relearning")
            ctx.cache.remember_search_format(other)
            return alt, other
        return rows, remembered

    log("  checking which date order the search field wants…")
    results = {fmt: _try_search(ctx, start, end, fmt) for fmt in SEARCH_FORMATS}
    counts = ", ".join(f"{fmt}: {len(results[fmt])}" for fmt in SEARCH_FORMATS)
    if all(not rows for rows in results.values()):
        log(f"  no bookings either way ({counts}); assuming {DEFAULT_SEARCH_FORMAT}")
        return [], DEFAULT_SEARCH_FORMAT
    best = max(SEARCH_FORMATS, key=lambda fmt: len(results[fmt]))
    log(f"  DateRange wants {best} ({counts})")
    ctx.cache.remember_search_format(best)
    return results[best], best


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------


def _collect_listings(
    ctx: Context, options: PullOptions, start: date, end: date, log: Log
) -> tuple[list[ListingRow], str]:
    rows: list[ListingRow] = []
    search_format = DEFAULT_SEARCH_FORMAT

    if options.source in ("completed", "both"):
        log("Fetching completed bookings…")
        completed, search_format = fetch_completed(ctx, start, end, log)
        log(f"  {len(completed)} row(s)")
        rows.extend(completed)

    if options.source in ("upcoming", "both"):
        log("Fetching upcoming bookings…")
        html, url = ctx.client.fetch_upcoming_html(ctx.cfg["site"]["upcoming_path"])
        upcoming = parse_listing(html, url=url, source="upcoming")
        log(f"  {len(upcoming)} row(s)")
        rows.extend(upcoming)

    return rows, search_format


def _dedupe(rows: list[ListingRow]) -> tuple[list[ListingRow], int]:
    """A booking in both listings is kept once, as completed."""
    seen: dict[str, ListingRow] = {}
    duplicates = 0
    for row in rows:
        if row.ref in seen:
            duplicates += 1
            continue
        seen[row.ref] = row
    return list(seen.values()), duplicates


def resolve_ordering(
    cfg: dict, rows: list[ListingRow], more_samples: Callable[[], list[str]] | None = None
) -> str:
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
            'Fix: set display_date_format = "DMY" (or "MDY") under [site] in '
            "config.toml, or widen --since so the batch includes a date after the "
            "12th of a month."
        )
    return "DMY"  # all dates were written out in words; ordering is irrelevant


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------


def pull(ctx: Context, options: PullOptions, log: Log = print) -> PullResult:
    log(f"Logging in to {ctx.client.base_url} as {ctx.client.username}…")
    ctx.client.login()

    search_start = options.since or DEFAULT_SEARCH_START
    search_end = options.until or date(date.today().year + 2, 12, 31)
    rows, search_format = _collect_listings(ctx, options, search_start, search_end, log)
    if not rows:
        return PullResult(bookings=[], search_format=search_format)

    rows, duplicates = _dedupe(rows)
    if duplicates:
        log(f"  {duplicates} booking(s) appeared in both listings; kept once")

    def wider_date_samples() -> list[str]:
        log("  dates in this batch are ambiguous; fetching a wider range to check…")
        wide = _try_search(
            ctx, DEFAULT_SEARCH_START, date(date.today().year + 2, 12, 31), search_format
        )
        return [row.event_date_raw for row in wide]

    ordering = resolve_ordering(ctx.cfg, rows, wider_date_samples)

    # Filter locally as well as in the search, because the server-side filter is
    # someone else's code and this one we can see.
    kept: list[ListingRow] = []
    for row in rows:
        event_date = parse_display_date(row.event_date_raw, ordering)
        if options.since and event_date < options.since:
            continue
        if options.until and event_date > options.until:
            continue
        kept.append(row)
    if len(kept) != len(rows):
        log(f"  {len(rows) - len(kept)} booking(s) outside the requested date range")
    rows = kept

    skipped: list[str] = []
    missing: list[str] = []
    if options.invoiced_refs:
        already = [row for row in rows if row.ref in options.invoiced_refs]
        log(f"  {len(already)} of these are already invoiced ({options.invoiced_mode})")
        if options.invoiced_mode == "exclude":
            rows = [row for row in rows if row.ref not in options.invoiced_refs]
            skipped = [row.ref for row in already]
            for row in already:
                log(f"    skipped {row.ref} ({row.event_date_raw})")
        seen_refs = {row.ref for row in rows} | {row.ref for row in already}
        missing = sorted(options.invoiced_refs - seen_refs)
        if missing:
            log(
                f"  note: {len(missing)} ref(s) on the invoiced list weren't in "
                f"this batch: {', '.join(missing)}"
            )

    if not rows:
        return PullResult(
            bookings=[],
            skipped_invoiced=skipped,
            missing_refs=missing,
            search_format=search_format,
            ordering=ordering,
        )

    event_dates = {row.ref: parse_display_date(row.event_date_raw, ordering) for row in rows}
    log(f"Fetching {len(rows)} detail page(s), {ctx.concurrency} at a time…")
    details = ctx.client.fetch_details(
        [row.ref for row in rows],
        detail_path=ctx.cfg["site"]["detail_path"],
        offset=int(ctx.cfg["site"]["detail_id_offset"]),
        cache=ctx.cache,
        event_dates=event_dates,
    )
    log(f"  {ctx.cache.summary()}")

    parsed_details = {ref: parse_detail(html, url=url) for ref, (html, url) in details.items()}

    built = build_bookings(
        rows,
        details,
        parsed_details,
        ordering=ordering,
        themes=ctx.themes,
        invoiced_refs=options.invoiced_refs,
        allow_blank_fees=options.allow_blank_fees,
    )

    return PullResult(
        bookings=built.bookings,
        warnings=built.warnings,
        unknown_packages=sorted(ctx.themes.unknown),
        skipped_invoiced=skipped,
        missing_refs=missing,
        search_format=search_format,
        ordering=ordering,
        cache_summary=ctx.cache.summary(),
    )
