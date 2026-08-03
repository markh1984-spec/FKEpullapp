"""Date and time handling.

The single most important thing in this file is `format_search_range`. Read the
comment on it before changing anything.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time

from .errors import DataError

# ---------------------------------------------------------------------------
# The search field
# ---------------------------------------------------------------------------

SEARCH_DATE_FORMAT = "%m/%d/%Y"


def format_search_date(value: date) -> str:
    """Format a date for the portal's DateRange search field.

    !!! US format, MM/DD/YYYY. !!!

    The portal is a UK site and shows UK dates everywhere, but the DateRange
    filter on /Bookings/MyBookingHistory is parsed as US MM/DD/YYYY. Sending
    UK order does not error — it silently matches fewer bookings, so the run
    "succeeds" with rows missing from the invoice.

    This is deliberately not configurable.
    """
    return value.strftime(SEARCH_DATE_FORMAT)


def format_search_range(start: date, end: date) -> str:
    """Build the DateRange value, e.g. "01/01/2025 - 12/31/2026" (US order)."""
    if end < start:
        raise DataError(f"date range end {end} is before start {start}")
    return f"{format_search_date(start)} - {format_search_date(end)}"


# ---------------------------------------------------------------------------
# Dates as displayed by the portal
# ---------------------------------------------------------------------------

_NUMERIC_DATE = re.compile(r"^(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{1,4})$")

# Formats whose meaning is unambiguous however you read them.
_TEXTUAL_FORMATS = (
    "%d %B %Y",
    "%d %b %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d-%b-%Y",
    "%Y-%m-%d",
)


def _clean(raw: str) -> str:
    """Strip a displayed date down to the date itself.

    Handles trailing times ("14/06/2026 13:00"), day names ("Sun 14/06/2026"),
    and ordinal suffixes ("14th June 2026").
    """
    text = " ".join(raw.split())
    text = re.sub(r"^(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?,?\s+", "", text, flags=re.I)
    text = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    text = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?$", "", text, flags=re.I)
    text = text.replace(",", " ")
    return " ".join(text.split())


def parse_textual_date(raw: str) -> date | None:
    """Parse a date whose format leaves no room for doubt. None if numeric."""
    text = _clean(raw)
    if not text:
        return None
    for fmt in _TEXTUAL_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _numeric_parts(raw: str) -> tuple[int, int, int] | None:
    match = _NUMERIC_DATE.match(_clean(raw))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def has_numeric_dates(samples: list[str]) -> bool:
    """True if any sample is a numeric date, i.e. one whose order matters."""
    return any(_numeric_parts(sample) for sample in samples)


def detect_ordering(samples: list[str]) -> str | None:
    """Work out whether numeric dates are DD/MM/YYYY or MM/DD/YYYY.

    Returns "DMY", "MDY", or None when the sample gives no evidence either way
    (every date has both leading numbers <= 12). Raises DataError if the sample
    contains contradictory evidence, which would mean mixed formats — we would
    rather stop than average it out.
    """
    dmy_evidence: list[str] = []
    mdy_evidence: list[str] = []
    for raw in samples:
        parts = _numeric_parts(raw)
        if not parts:
            continue
        first, second, _third = parts
        if first > 12 and second <= 12:
            dmy_evidence.append(raw)
        elif second > 12 and first <= 12:
            mdy_evidence.append(raw)

    if dmy_evidence and mdy_evidence:
        raise DataError(
            "The booking dates are not in a consistent format: "
            f"{dmy_evidence[0]!r} can only be day/month, but {mdy_evidence[0]!r} "
            "can only be month/day. Refusing to guess — check the portal output "
            "and set display_date_format in config.toml if you know better."
        )
    if dmy_evidence:
        return "DMY"
    if mdy_evidence:
        return "MDY"
    return None


def parse_display_date(raw: str, ordering: str) -> date:
    """Parse a date as shown by the portal. `ordering` is "DMY" or "MDY"."""
    text = _clean(raw)
    if not text:
        raise DataError("empty date")

    textual = parse_textual_date(raw)
    if textual is not None:
        return textual

    parts = _numeric_parts(raw)
    if parts is None:
        raise DataError(f"could not read {raw!r} as a date")

    first, second, third = parts
    if first > 31:  # YYYY/MM/DD
        year, month, day = first, second, third
    elif ordering == "MDY":
        month, day, year = first, second, third
    else:
        day, month, year = first, second, third

    if year < 100:  # two-digit year
        year += 2000
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise DataError(f"{raw!r} is not a real date (read as {ordering}): {exc}") from exc


def format_uk(value: date) -> str:
    """DD/MM/YYYY — how dates appear in the CSVs."""
    return value.strftime("%d/%m/%Y")


def parse_user_date(raw: str) -> date:
    """Parse a date typed on the command line (--since / --until).

    Accepts DD/MM/YYYY (what you'd naturally type) and YYYY-MM-DD.
    """
    text = raw.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise DataError(
        f"could not read {raw!r} as a date — use DD/MM/YYYY (e.g. 01/04/2026) "
        "or YYYY-MM-DD"
    )


# ---------------------------------------------------------------------------
# Times and durations
# ---------------------------------------------------------------------------

_TIME_PATTERNS = (
    "%H:%M:%S",
    "%H:%M",
    "%I:%M %p",
    "%I:%M%p",
    "%I %p",
    "%I%p",
    "%H.%M",
)


def parse_time(raw: str) -> time | None:
    """Parse a start/end time. Returns None for blank or unreadable input."""
    text = " ".join(raw.split()).upper().replace(".", ":") if raw else ""
    # Undo the replace for the "%H.%M" style by trying both spellings below.
    candidates = {text, " ".join(raw.split()).upper()} - {""}
    for candidate in candidates:
        normalised = re.sub(r"\s*(AM|PM)$", r" \1", candidate)
        for fmt in _TIME_PATTERNS:
            try:
                return datetime.strptime(normalised, fmt).time()
            except ValueError:
                continue
    return None


def duration_between(start: time, end: time) -> int:
    """Minutes between two times. Negative spans are rejected by the caller."""
    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    return end_minutes - start_minutes


def format_duration(minutes: int) -> str:
    """Render minutes as e.g. "1h 30m", "2h", "45m"."""
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"
