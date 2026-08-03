"""Turning parsed pages into the two CSVs."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from .dates import duration_between, format_duration, format_uk, parse_display_date, parse_time
from .errors import DataError
from .money import commission, format_money, parse_money
from .parse import ListingRow, assert_ref_matches
from .themes import ThemeMapper


@dataclass
class Booking:
    ref: str
    event_date: date
    package: str
    theme: str
    fee: Decimal | None
    listing: ListingRow
    detail: dict[str, str]
    source: str
    invoiced: bool = False

    @property
    def duration_minutes(self) -> int | None:
        start = parse_time(self.detail.get("start_time", ""))
        end = parse_time(self.detail.get("end_time", ""))
        if start is None or end is None:
            return None
        minutes = duration_between(start, end)
        return minutes if minutes > 0 else None

    def commission(self, *, percent: int, rounding: str) -> Decimal:
        if self.fee is None:
            return Decimal(0)
        return commission(self.fee, percent=percent, rounding=rounding)


@dataclass
class BuildResult:
    bookings: list[Booking]
    warnings: list[str] = field(default_factory=list)


def build_bookings(
    rows: list[ListingRow],
    details: dict[str, tuple[str, str]],
    parsed_details: dict[str, dict[str, str]],
    *,
    ordering: str,
    themes: ThemeMapper,
    invoiced_refs: set[str],
    allow_blank_fees: bool,
) -> BuildResult:
    """Cross-check listing rows against detail pages and assemble bookings."""
    warnings: list[str] = []
    bookings: list[Booking] = []
    blank_fees: list[str] = []

    for row in rows:
        detail = parsed_details[row.ref]
        url = details[row.ref][1]

        assert_ref_matches(detail, row.ref, url=url)

        event_date = parse_display_date(row.event_date_raw, ordering)
        detail_date = parse_display_date(detail["event_date"], ordering)
        if detail_date != event_date:
            raise DataError(
                f"booking {row.ref}: the bookings table says the party is on "
                f"{format_uk(event_date)} but the detail page says "
                f"{format_uk(detail_date)}. Refusing to guess which is right."
            )

        confirmed_raw = detail.get("confirmed_date", "")
        if confirmed_raw:
            try:
                detail = {
                    **detail,
                    "confirmed_date": format_uk(parse_display_date(confirmed_raw, ordering)),
                }
            except DataError:
                warnings.append(
                    f"{row.ref}: couldn't read the booking confirmed date "
                    f"{confirmed_raw!r}; leaving it as-is in details.csv."
                )

        package = detail.get("package") or row.package
        if row.package and detail.get("package") and \
                " ".join(row.package.split()).lower() != " ".join(package.split()).lower():
            warnings.append(
                f"{row.ref}: package differs between the table ({row.package!r}) "
                f"and the detail page ({package!r}); using the detail page."
            )

        fee = parse_money(detail.get("balance", ""), field=f"balance for booking {row.ref}")
        if fee is None:
            blank_fees.append(row.ref)
        elif fee < 0:
            raise DataError(
                f"booking {row.ref}: balance is negative ({fee}). That isn't a fee "
                "this tool knows how to invoice."
            )

        booking = Booking(
            ref=row.ref,
            event_date=event_date,
            package=package,
            theme=themes.theme_for(package),
            fee=fee,
            listing=row,
            detail=detail,
            source=row.source,
            invoiced=row.ref in invoiced_refs,
        )

        if booking.duration_minutes is None and (
            detail.get("start_time") and detail.get("end_time")
        ):
            warnings.append(
                f"{row.ref}: couldn't work out a duration from start "
                f"{detail['start_time']!r} and end {detail['end_time']!r}."
            )

        bookings.append(booking)

    if blank_fees and not allow_blank_fees:
        raise DataError(
            "These bookings have a blank Balance on their detail page, so there "
            "is no fee to take a percentage of:\n  "
            + "\n  ".join(sorted(blank_fees))
            + "\nFix them on the portal, or re-run with --allow-blank-fees to "
            "list them at zero (they'll be obvious in the CSV)."
        )
    if blank_fees:
        warnings.append(
            f"{len(blank_fees)} booking(s) have a blank balance and are shown "
            f"with an empty fee: {', '.join(sorted(blank_fees))}"
        )

    bookings.sort(key=lambda b: (b.event_date, b.ref))
    return BuildResult(bookings=bookings, warnings=warnings)


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------


def _write(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write_invoice(
    path: Path,
    bookings: list[Booking],
    *,
    percent: int,
    rounding: str,
    total_from_rounded_lines: bool,
    mark_invoiced: bool,
) -> None:
    """invoice.csv — one line per party, with the total at the bottom."""
    header = ["#", "Booking Ref", "Date", "Theme", "Fee", f"{percent}%"]
    if mark_invoiced:
        header.append("Already Invoiced")

    rows: list[list[str]] = []
    fee_total = Decimal(0)
    cut_total = Decimal(0)
    new_fee_total = Decimal(0)
    new_cut_total = Decimal(0)

    for index, booking in enumerate(bookings, start=1):
        cut = booking.commission(percent=percent, rounding=rounding)
        fee = booking.fee or Decimal(0)
        row = [
            str(index),
            booking.ref,
            format_uk(booking.event_date),
            booking.theme,
            format_money(fee) if booking.fee is not None else "",
            format_money(cut) if booking.fee is not None else "",
        ]
        if mark_invoiced:
            row.append("YES" if booking.invoiced else "")
        rows.append(row)

        fee_total += fee
        cut_total += cut
        if not booking.invoiced:
            new_fee_total += fee
            new_cut_total += cut

    def total_row(label: str, fees: Decimal, cuts: Decimal) -> list[str]:
        total_cut = cuts if total_from_rounded_lines else commission(
            fees, percent=percent, rounding=rounding
        )
        row = [label, "", "", "", format_money(fees), format_money(total_cut)]
        if mark_invoiced:
            row.append("")
        return row

    if mark_invoiced:
        rows.append(total_row("TOTAL (not yet invoiced)", new_fee_total, new_cut_total))
        rows.append(total_row("TOTAL (all rows)", fee_total, cut_total))
    else:
        rows.append(total_row("TOTAL", fee_total, cut_total))

    _write(path, header, rows)


def write_details(path: Path, bookings: list[Booking]) -> None:
    """details.csv — the per-booking information, for reference."""
    header = [
        "Booking Ref",
        "Booking Confirmed Date",
        "Customer Name",
        "Phone",
        "Package",
        "Venue",
        "Party Occasion",
        "Duration",
    ]
    rows = []
    for booking in bookings:
        minutes = booking.duration_minutes
        rows.append(
            [
                booking.ref,
                booking.detail.get("confirmed_date", ""),
                booking.detail.get("customer", ""),
                booking.detail.get("phone", ""),
                booking.package,
                booking.detail.get("address_line_1", ""),
                booking.detail.get("party_occasion", ""),
                format_duration(minutes) if minutes is not None else "",
            ]
        )
    _write(path, header, rows)


def load_invoiced_refs(path: str | Path) -> set[str]:
    """Read the list of refs already invoiced. One per line; # comments allowed."""
    file_path = Path(path)
    if not file_path.exists():
        raise DataError(f"invoiced-refs file not found: {file_path}")
    refs: set[str] = set()
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        for token in line.replace(",", " ").split():
            refs.add(token.strip())
    return refs
