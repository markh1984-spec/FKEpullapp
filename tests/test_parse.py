"""Parser tests, including the ways the portal's HTML could go wrong on us."""

import pytest

from fkepull import parse as parse_module
from fkepull.errors import DataError, SchemaError
from fkepull.parse import (
    assert_ref_matches,
    find_login_form,
    parse_detail,
    parse_listing,
)
from tests.fake_portal import DEFAULT_BOOKINGS, LOGIN_HTML, _detail_html, _rows_html


def test_parse_listing_reads_every_row():
    rows = parse_listing(_rows_html(DEFAULT_BOOKINGS), url="/x", source="completed")
    assert len(rows) == len(DEFAULT_BOOKINGS)
    assert rows[0].ref == "100-26501"
    assert rows[0].client == "Smith"
    assert rows[0].event_date_raw == "14/06/2025"
    assert rows[0].postcode == "LS1 4AB"


def test_parse_listing_handles_an_empty_result_set():
    assert parse_listing(_rows_html([]), url="/x", source="completed") == []


def test_parse_listing_tolerates_extra_columns():
    html = _rows_html(DEFAULT_BOOKINGS[:1])
    html = html.replace("<th>Postcode</th>", "<th>Postcode</th><th>Actions</th>")
    html = html.replace("<td>LS1 4AB</td>", "<td>LS1 4AB</td><td><a href='#'>View</a></td>")
    rows = parse_listing(html, url="/x", source="completed")
    assert rows[0].postcode == "LS1 4AB"


def test_parse_listing_fails_loudly_when_a_column_disappears():
    html = _rows_html(DEFAULT_BOOKINGS[:1]).replace("<th>Booking Ref</th>", "<th>Job Number</th>")
    with pytest.raises(SchemaError) as exc:
        parse_listing(html, url="/Bookings/MyBookingHistory", source="completed")
    assert "bookings table" in str(exc.value)
    assert "Job Number" in str(exc.value)


def test_parse_listing_fails_when_a_row_is_short():
    html = _rows_html(DEFAULT_BOOKINGS[:1]).replace("<td>LS1 4AB</td>", "")
    with pytest.raises(SchemaError):
        parse_listing(html, url="/x", source="completed")


def test_parse_listing_fails_on_a_ref_that_is_not_a_ref():
    html = _rows_html(DEFAULT_BOOKINGS[:1]).replace("<td>100-26501</td>", "<td>pending</td>")
    with pytest.raises(SchemaError) as exc:
        parse_listing(html, url="/x", source="completed")
    assert "100-26528" in str(exc.value)  # the message shows the expected shape


def test_parse_listing_notices_the_login_page():
    with pytest.raises(SchemaError) as exc:
        parse_listing(LOGIN_HTML.format(error=""), url="/x", source="completed")
    assert "not authenticated" in str(exc.value)


# --- detail pages -----------------------------------------------------------

def test_parse_detail_reads_all_three_layouts():
    booking = DEFAULT_BOOKINGS[4]  # has an address line 2
    detail = parse_detail(_detail_html(booking), url="/Bookings/Details/16505")
    assert detail["booking_ref"] == "100-26505"          # bootstrap row + label
    assert detail["customer"] == "Nita Patel"
    assert detail["package"] == booking.package          # dl/dt/dd
    assert detail["address_line_2"] == "Flat 2"
    assert detail["balance"] == "£175.00"                # th/td table
    assert detail["party_occasion"] == "Birthday"
    assert detail["confirmed_date"] == "01/02/2026"


def test_parse_detail_leaves_empty_fields_empty_rather_than_borrowing_the_next_one():
    booking = DEFAULT_BOOKINGS[0]  # no address line 2, no county
    detail = parse_detail(_detail_html(booking), url="/x")
    assert detail["address_line_2"] == ""
    assert detail["county"] == ""
    assert detail["postcode"] == "LS1 4AB"   # the field after the empty ones is intact
    assert detail["internal_notes"] == ""
    assert detail["playlist"] == ""


def test_parse_detail_fails_when_balance_vanishes():
    html = _detail_html(DEFAULT_BOOKINGS[0]).replace("Balance", "Amount Due")
    with pytest.raises(SchemaError) as exc:
        parse_detail(html, url="/Bookings/Details/16501")
    assert "balance" in str(exc.value)
    assert "labels we did find" in str(exc.value)


def test_parse_detail_allows_a_blank_balance_here_and_complains_later():
    html = _detail_html(DEFAULT_BOOKINGS[0]).replace("£150.00", "")
    detail = parse_detail(html, url="/x")
    assert detail["balance"] == ""


def test_assert_ref_matches_catches_the_wrong_booking():
    with pytest.raises(DataError) as exc:
        assert_ref_matches({"booking_ref": "100-26502"}, "100-26501", url="/Bookings/Details/16501")
    assert "no longer holds" in str(exc.value)


def test_assert_ref_matches_passes_when_it_matches():
    assert_ref_matches({"booking_ref": "100-26501"}, "100-26501", url="/x") is None


# --- login form -------------------------------------------------------------

def test_find_login_form_keeps_the_antiforgery_token():
    form = find_login_form(LOGIN_HTML.format(error=""), url="http://x/Account/Login")
    assert form.username_field == "UserName"
    assert form.password_field == "Password"
    assert form.fields["__RequestVerificationToken"]
    assert form.fields["ReturnUrl"] == "/"
    assert "RememberMe" not in form.fields  # unchecked checkbox isn't posted


def test_find_login_form_complains_when_there_is_no_form():
    with pytest.raises(SchemaError):
        find_login_form("<html><body>nothing here</body></html>", url="http://x/")


# --- it must work without lxml too -------------------------------------------

@pytest.fixture
def without_lxml(monkeypatch):
    """Pretend lxml isn't installed, as on a Mac with no developer tools."""
    monkeypatch.setattr(parse_module, "_PARSERS", ("html.parser",))


def test_listing_parses_with_pythons_own_parser(without_lxml):
    rows = parse_listing(_rows_html(DEFAULT_BOOKINGS), url="/x", source="completed")
    assert len(rows) == len(DEFAULT_BOOKINGS)
    assert rows[0].ref == "100-26501"
    assert rows[0].event_date_raw == "14/06/2025"


def test_detail_parses_with_pythons_own_parser(without_lxml):
    detail = parse_detail(_detail_html(DEFAULT_BOOKINGS[4]), url="/x")
    assert detail["booking_ref"] == "100-26505"
    assert detail["customer"] == "Nita Patel"
    assert detail["balance"] == "£175.00"
    assert detail["address_line_2"] == "Flat 2"
    assert detail["county"] == ""


def test_a_missing_parser_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(parse_module, "_PARSERS", ("no-such-parser",))
    with pytest.raises(SchemaError) as exc:
        parse_listing("<html></html>", url="/x", source="completed")
    assert "couldn't parse the page" in str(exc.value)
