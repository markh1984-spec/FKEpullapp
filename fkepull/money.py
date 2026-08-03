"""Money parsing and the 20% calculation.

The Balance field on a detail page is the fee paid to you, and is the only
basis for the percentage. FKE's customer deposit is a separate thing that never
appears on these pages and must not enter into any of this.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP

from .errors import DataError

_ALLOWED = re.compile(r"^[£$€\s]*-?[\d,]+(\.\d+)?[\s]*$")
_STRIP = re.compile(r"[£$€,\s]")

ROUNDING_MODES = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
}


def parse_money(raw: str, *, field: str = "amount") -> Decimal | None:
    """Parse "£1,234.50" and friends into a Decimal.

    Returns None for a blank value or a placeholder dash. Anything else that
    doesn't look like money is an error — we never fall back to zero, because a
    silent zero is an under-charged invoice line.
    """
    text = (raw or "").strip()
    if text in ("", "-", "–", "—", "N/A", "n/a"):
        return None
    if not _ALLOWED.match(text):
        raise DataError(f"{field} {raw!r} doesn't look like an amount of money")
    try:
        return Decimal(_STRIP.sub("", text))
    except InvalidOperation as exc:
        raise DataError(f"{field} {raw!r} doesn't look like an amount of money") from exc


def format_money(value: Decimal) -> str:
    """Two decimal places, no currency symbol — this is going into a CSV."""
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}"


def commission(fee: Decimal, *, percent: int = 20, rounding: str = "half_up") -> Decimal:
    """Your cut of a fee, rounded to whole pounds.

    Default rounding is half-up (49.50 -> 50), which is how you round by hand.
    Python's built-in round() would give 50 for 49.5 but 48 for 48.5, which is
    not what anyone expects on an invoice.
    """
    try:
        mode = ROUNDING_MODES[rounding]
    except KeyError:
        raise DataError(
            f"unknown rounding mode {rounding!r} in config — "
            f"use one of: {', '.join(sorted(ROUNDING_MODES))}"
        ) from None
    return (fee * Decimal(percent) / Decimal(100)).quantize(Decimal("1"), rounding=mode)
