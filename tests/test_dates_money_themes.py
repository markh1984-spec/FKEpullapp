"""The bits where a quiet mistake costs money."""

from datetime import date
from decimal import Decimal

import pytest

from fkepull.dates import (
    detect_ordering,
    format_duration,
    format_search_range,
    format_uk,
    parse_display_date,
    parse_time,
    parse_user_date,
)
from fkepull.errors import ConfigError, DataError
from fkepull.money import commission, parse_money
from fkepull.themes import ThemeMapper


# --- the search field is US format -----------------------------------------

def test_search_range_is_us_format():
    # This is the exact example from the portal: 1 Jan 2025 to 31 Dec 2026.
    assert format_search_range(date(2025, 1, 1), date(2026, 12, 31)) == "01/01/2025 - 12/31/2026"


def test_search_range_puts_month_first_even_when_it_looks_uk():
    # 6 June is the same either way; 14 June is not. Month leads.
    assert format_search_range(date(2026, 6, 14), date(2026, 6, 14)) == "06/14/2026 - 06/14/2026"


def test_search_range_rejects_backwards_range():
    with pytest.raises(DataError):
        format_search_range(date(2026, 1, 2), date(2026, 1, 1))


# --- reading the dates the portal shows ------------------------------------

def test_detect_ordering_uk():
    assert detect_ordering(["14/06/2026", "03/07/2026"]) == "DMY"


def test_detect_ordering_us():
    assert detect_ordering(["06/14/2026", "07/03/2026"]) == "MDY"


def test_detect_ordering_gives_up_when_ambiguous():
    assert detect_ordering(["03/07/2026", "01/02/2026"]) is None


def test_detect_ordering_refuses_mixed_evidence():
    with pytest.raises(DataError):
        detect_ordering(["14/06/2026", "06/14/2026"])


@pytest.mark.parametrize(
    "raw,ordering,expected",
    [
        ("14/06/2026", "DMY", date(2026, 6, 14)),
        ("06/14/2026", "MDY", date(2026, 6, 14)),
        ("14 June 2026", "MDY", date(2026, 6, 14)),  # words beat the ordering hint
        ("Sun 14/06/2026 13:00", "DMY", date(2026, 6, 14)),
        ("14th June 2026", "DMY", date(2026, 6, 14)),
        ("2026-06-14", "DMY", date(2026, 6, 14)),
    ],
)
def test_parse_display_date(raw, ordering, expected):
    assert parse_display_date(raw, ordering) == expected


def test_parse_display_date_rejects_impossible():
    with pytest.raises(DataError):
        parse_display_date("31/02/2026", "DMY")


def test_parse_user_date_is_uk():
    assert parse_user_date("01/04/2026") == date(2026, 4, 1)
    assert parse_user_date("2026-04-01") == date(2026, 4, 1)


def test_format_uk():
    assert format_uk(date(2026, 6, 14)) == "14/06/2026"


# --- times ------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["14:00", "2:00 PM", "2:00PM", "14:00:00", "14.00"])
def test_parse_time_variants(raw):
    parsed = parse_time(raw)
    assert parsed is not None and parsed.hour == 14 and parsed.minute == 0


def test_parse_time_blank():
    assert parse_time("") is None
    assert parse_time("tbc") is None


def test_format_duration():
    assert format_duration(90) == "1h 30m"
    assert format_duration(120) == "2h"
    assert format_duration(45) == "45m"


# --- money ------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("£150.00", Decimal("150.00")), ("1,234.50", Decimal("1234.50")), ("£99", Decimal("99"))],
)
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


def test_parse_money_blank_is_none_not_zero():
    assert parse_money("") is None
    assert parse_money("-") is None


def test_parse_money_rejects_nonsense():
    with pytest.raises(DataError):
        parse_money("see notes")


def test_commission_rounds_half_up_not_bankers():
    # 162.50 -> 32.50. Rounded by hand that's 33; Python's round() says 32.
    assert commission(Decimal("162.50")) == Decimal("33")
    assert commission(Decimal("197.50")) == Decimal("40")
    assert commission(Decimal("150.00")) == Decimal("30")
    assert commission(Decimal("120.50")) == Decimal("24")
    assert commission(Decimal("99.99")) == Decimal("20")


def test_commission_percent_is_configurable():
    assert commission(Decimal("100"), percent=25) == Decimal("25")


# --- themes -----------------------------------------------------------------

def _mapper():
    import tomllib
    from pathlib import Path

    with (Path(__file__).resolve().parent.parent / "config.toml").open("rb") as handle:
        return ThemeMapper.from_config(tomllib.load(handle))


@pytest.mark.parametrize(
    "package,theme",
    [
        ("FUN Kids Party (Indoor Use)", "party"),
        ("Outdoor FUN Party", "party"),
        ("FUN School Disco", "school disco"),
        ("FUN Neon Glow Disco", "glow"),
        ("Fun Kids Party + Theme / Spiderman", "spiderman"),
        ("fun kids party (indoor use)", "party"),          # case doesn't matter
        ("FUN  Kids   Party (Indoor Use)", "party"),       # nor does spacing
        ("Fun Kids Party + Theme / Batman", "batman"),     # new themes just work
    ],
)
def test_theme_mapping(package, theme):
    assert _mapper().theme_for(package) == theme


def test_unknown_package_is_recorded_for_warning():
    mapper = _mapper()
    assert mapper.theme_for("FUN Toddler Sensory Session") == "fun toddler sensory session"
    assert "FUN Toddler Sensory Session" in mapper.unknown


def test_bad_theme_regex_is_a_config_error():
    with pytest.raises(ConfigError):
        ThemeMapper.from_config({"themes": {"rules": [{"pattern": "([", "theme": "x"}]}})
