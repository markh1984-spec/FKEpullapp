"""HTML parsing.

This is someone else's system with no API contract, so every function here is
written on the assumption that the markup may change without warning. The rule
throughout: recognise what we expect, and raise SchemaError with a description
of what we actually found otherwise. Never return a half-understood row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from .errors import DataError, SchemaError

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

BOOKING_REF_RE = re.compile(r"\b(\d{2,5})-(\d{3,9})\b")


# lxml is quicker and more forgiving of malformed markup, but it has to be
# compiled, so it may be missing on a machine without developer tools. Python's
# own parser handles these pages fine — it is just slower.
_PARSERS = ("lxml", "html.parser")


def soup_of(html: str) -> BeautifulSoup:
    problem = None
    for parser in _PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception as exc:  # the parser isn't installed
            problem = exc
    raise SchemaError(f"couldn't parse the page: {problem}")


def norm(text: str | None) -> str:
    """Collapse whitespace and drop the punctuation labels tend to carry."""
    cleaned = " ".join((text or "").replace("\xa0", " ").split())
    return cleaned.strip().strip(":*").strip().lower()


def cell_text(tag: Tag) -> str:
    """Visible text of a cell, with <br> turned into newlines for notes."""
    text = tag.get_text("\n", strip=True).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def looks_like_login_page(html: str) -> bool:
    soup = soup_of(html)
    return soup.find("input", attrs={"type": "password"}) is not None


# ---------------------------------------------------------------------------
# The bookings table (both /Bookings/MyBookingHistory and /Bookings/MyBookings)
# ---------------------------------------------------------------------------

LISTING_COLUMNS: dict[str, tuple[str, ...]] = {
    "client": ("client", "client name", "customer", "customer name"),
    "package": ("package", "package name"),
    "ref": ("booking ref", "booking reference", "ref", "reference"),
    "event_date": ("event date", "party date", "date"),
    "postcode": ("postcode", "post code", "post-code"),
}

_NO_RESULTS_MARKERS = (
    "no records",
    "no bookings",
    "no results",
    "no data available",
    "nothing to display",
)


@dataclass(frozen=True)
class ListingRow:
    """One row of a bookings table, still entirely as strings."""

    client: str
    package: str
    ref: str
    event_date_raw: str
    postcode: str
    source: str


def _header_cells(table: Tag) -> tuple[list[str], list[str], Tag | None]:
    """Header cells as (normalised, as-written, the row they came from)."""
    row = None
    thead = table.find("thead")
    if thead:
        row = thead.find("tr")
    if row is None:
        row = table.find("tr")
    if row is None:
        return [], [], None
    cells = row.find_all(["th", "td"])
    return (
        [norm(cell.get_text()) for cell in cells],
        [" ".join(cell.get_text(" ", strip=True).split()) for cell in cells],
        row,
    )


def _map_columns(headers: list[str]) -> dict[str, int] | None:
    """Match header cells to the columns we need. None if any is missing."""
    mapping: dict[str, int] = {}
    for key, aliases in LISTING_COLUMNS.items():
        for index, header in enumerate(headers):
            if header in aliases and index not in mapping.values():
                mapping[key] = index
                break
        else:
            return None
    return mapping


def parse_listing(html: str, *, url: str, source: str) -> list[ListingRow]:
    """Parse a bookings table into rows, or explain why we can't."""
    soup = soup_of(html)

    if looks_like_login_page(html):
        raise SchemaError(
            f"{url} returned the login page — the session is not authenticated. "
            "Check FKE_USER / FKE_PASS."
        )

    matches: list[tuple[Tag, dict[str, int], Tag | None]] = []
    seen_headers: list[list[str]] = []
    for table in soup.find_all("table"):
        headers, raw_headers, header_row = _header_cells(table)
        if headers:
            seen_headers.append(raw_headers)
        mapping = _map_columns(headers)
        if mapping:
            matches.append((table, mapping, header_row))

    if not matches:
        page_text = norm(soup.get_text(" "))
        if any(marker in page_text for marker in _NO_RESULTS_MARKERS):
            return []
        found = "; ".join(" | ".join(h) for h in seen_headers if h) or "no tables with headers"
        raise SchemaError(
            f"{url}: could not find the bookings table.\n"
            f"  expected columns: {', '.join(k.replace('_', ' ') for k in LISTING_COLUMNS)}\n"
            f"  tables found on the page: {found}\n"
            "The portal's HTML has probably changed. Stopping rather than "
            "guessing which columns are which."
        )
    if len(matches) > 1:
        raise SchemaError(
            f"{url}: found {len(matches)} tables that look like the bookings "
            "table. Refusing to pick one — the page layout has changed."
        )

    table, mapping, header_row = matches[0]
    needed = max(mapping.values()) + 1

    body = table.find("tbody") or table
    rows: list[ListingRow] = []
    for tr in body.find_all("tr"):
        if header_row is not None and tr is header_row:
            continue
        if tr.find("th") and not tr.find("td"):
            continue  # a header row inside tbody
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        if len(cells) == 1:
            text = norm(cells[0].get_text())
            if any(marker in text for marker in _NO_RESULTS_MARKERS):
                continue
            raise SchemaError(
                f"{url}: unexpected single-cell row in the bookings table: "
                f"{cells[0].get_text(' ', strip=True)!r}"
            )
        if len(cells) < needed:
            raise SchemaError(
                f"{url}: a row in the bookings table has {len(cells)} cells but "
                f"we need at least {needed}. Row: "
                f"{[c.get_text(' ', strip=True) for c in cells]!r}"
            )

        values = {key: cell_text(cells[index]) for key, index in mapping.items()}
        ref = values["ref"].strip()
        if not BOOKING_REF_RE.search(ref):
            raise SchemaError(
                f"{url}: {ref!r} is in the booking ref column but doesn't look "
                "like a booking ref (expected something like 100-26528)."
            )
        rows.append(
            ListingRow(
                client=values["client"],
                package=values["package"],
                ref=BOOKING_REF_RE.search(ref).group(0),
                event_date_raw=values["event_date"],
                postcode=values["postcode"],
                source=source,
            )
        )

    return rows


# ---------------------------------------------------------------------------
# The detail page
# ---------------------------------------------------------------------------

# canonical name -> spellings of the label we'll accept
DETAIL_LABELS: dict[str, tuple[str, ...]] = {
    "booking_ref": ("booking ref", "booking reference", "reference", "ref"),
    "customer": ("customer", "customer name", "client", "client name"),
    "phone": ("phone", "telephone", "phone number", "contact number", "mobile"),
    "entertainer": ("entertainer",),
    "package": ("package", "package name"),
    "event_date": ("event date", "party date"),
    "start_time": ("start time",),
    "end_time": ("end time", "finish time"),
    "address_line_1": ("address line 1", "address 1"),
    "address_line_2": ("address line 2", "address 2"),
    "town_city": ("town/city", "town / city", "town", "city"),
    "county": ("county",),
    "postcode": ("postcode", "post code"),
    "balance": ("balance",),
    "internal_notes": ("internal notes",),
    "party_occasion": ("party occasion", "occasion"),
    "parking_info": (
        "parking/location information",
        "parking / location information",
        "parking information",
        "parking/location info",
        "parking",
    ),
    "playlist": ("playlist", "play list"),
    "other_notes": ("other party notes", "other notes"),
    "confirmed_date": ("booking confirmed date", "confirmed date", "booking confirmed"),
}

# Missing any of these means we cannot produce trustworthy numbers, so we stop.
CRITICAL_FIELDS = ("booking_ref", "event_date", "package")
# Balance may legitimately be blank on the portal, but the label itself
# disappearing means the page layout changed and we'd read fees from nowhere.
REQUIRED_LABELS = ("balance",)

_ALL_LABEL_SPELLINGS = {spelling for spellings in DETAIL_LABELS.values() for spelling in spellings}

_LABEL_LOOKUP: dict[str, str] = {}
for _key, _spellings in DETAIL_LABELS.items():
    for _spelling in _spellings:
        # First declaration wins, so "customer" maps to customer, not client.
        _LABEL_LOOKUP.setdefault(_spelling, _key)


def _is_label_text(text: str) -> bool:
    return text in _ALL_LABEL_SPELLINGS


def _inline_value(tag: Tag, spelling: str) -> str | None:
    """Handle markup like <span>Customer: Jane Smith</span>."""
    raw = " ".join(tag.get_text(" ", strip=True).replace("\xa0", " ").split())
    lowered = raw.lower()
    if not lowered.startswith(spelling):
        return None
    remainder = raw[len(spelling):].lstrip()
    if not remainder or remainder[0] not in ":-–":
        return None
    value = remainder[1:].strip()
    return value or None


def _first_tag(siblings, names: tuple[str, ...] | None = None) -> Tag | None:
    for sibling in siblings:
        if isinstance(sibling, Tag) and (names is None or sibling.name in names):
            return sibling
    return None


def _structural_partner(tag: Tag) -> Tag | None:
    """The element that, by the shape of the markup, holds this label's value.

    <dt> -> its <dd>, <th> -> the next cell, a <label> in a column -> the next
    column. This is authoritative even when it turns out to be empty, which is
    how "Address Line 2" with nothing in it stays empty instead of helping
    itself to the town on the next line.
    """
    if tag.name == "dt":
        return _first_tag(tag.next_siblings, ("dd",))
    if tag.name in ("th", "td"):
        return _first_tag(tag.next_siblings, ("td", "th"))

    direct = _first_tag(tag.next_siblings)
    if direct is not None:
        return direct

    parent = tag.parent
    if isinstance(parent, Tag):
        if parent.name in ("th", "td"):
            return _first_tag(parent.next_siblings, ("td", "th"))
        if parent.name == "dt":
            return _first_tag(parent.next_siblings, ("dd",))
        if parent.name in ("label", "strong", "b", "span", "div", "p"):
            return _first_tag(parent.next_siblings)
    return None


def _following_value(tag: Tag) -> str | None:
    """The value belonging to a label element, or None if there isn't one."""
    # <b>Phone:</b> 07700 900123 — the value is a bare text node.
    for sibling in tag.next_siblings:
        if isinstance(sibling, Tag):
            break
        text = " ".join(str(sibling).split())
        if text:
            return text

    partner = _structural_partner(tag)
    if partner is not None:
        text = cell_text(partner)
        if _is_label_text(norm(text)):
            # The next thing on the page is another label, so this field is
            # simply empty. Don't steal the neighbour's name.
            return None
        return text or None

    # Nothing structural to go on: take the first sibling that has any text.
    for sibling in tag.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        text = cell_text(sibling)
        if not text:
            continue
        if _is_label_text(norm(text)):
            return None
        return text
    return None


def parse_detail(html: str, *, url: str) -> dict[str, str]:
    """Pull the labelled fields off a booking detail page.

    Returns canonical name -> value. A label that is present but has no value
    comes back as an empty string; a label that is missing entirely from the
    page is either a hard error (if it's one we need for the maths) or an empty
    string plus a warning recorded in the `_missing` key.
    """
    if looks_like_login_page(html):
        raise SchemaError(
            f"{url} returned the login page — the session is not authenticated."
        )

    soup = soup_of(html)
    # key -> label spelling -> values seen for that exact spelling
    found: dict[str, dict[str, list[str]]] = {}
    seen_labels: set[str] = set()

    for tag in soup.find_all(True):
        if tag.name in ("script", "style", "html", "body", "head"):
            continue
        text = norm(tag.get_text(" "))
        if not text or len(text) > 120:
            continue

        spelling = None
        if _is_label_text(text):
            spelling = text
        else:
            for candidate in _ALL_LABEL_SPELLINGS:
                if text.startswith(candidate) and len(text) > len(candidate):
                    if text[len(candidate)] in ":-–" or text[len(candidate)] == " ":
                        spelling = candidate
                        break
            if spelling is None:
                continue

        # Only take the innermost element carrying this label, so a wrapping
        # <div> doesn't shadow the <dt> inside it.
        if any(isinstance(child, Tag) and norm(child.get_text(" ")).startswith(spelling)
               for child in tag.find_all(True, recursive=True)):
            continue

        key = _LABEL_LOOKUP[spelling]
        seen_labels.add(key)

        value = _inline_value(tag, spelling) if text != spelling else None
        if value is None and text == spelling:
            value = _following_value(tag)
        found.setdefault(key, {}).setdefault(spelling, [])
        if value:
            found[key][spelling].append(value)

    values: dict[str, str] = {}
    for key, spellings in DETAIL_LABELS.items():
        if key not in seen_labels:
            continue
        chosen = ""
        for spelling in spellings:  # declaration order is preference order
            distinct = list(dict.fromkeys(found.get(key, {}).get(spelling, [])))
            if len(distinct) > 1:
                raise SchemaError(
                    f"{url}: the label {spelling!r} appears more than once with "
                    f"different values ({distinct!r}). Can't tell which is the "
                    "real one."
                )
            if distinct and not chosen:
                chosen = distinct[0]
        values[key] = chosen

    # A ref printed as bare text with no label at all still lets us verify the
    # page we were given is the page we asked for.
    if not values.get("booking_ref"):
        match = BOOKING_REF_RE.search(soup.get_text(" "))
        if match:
            values["booking_ref"] = match.group(0)
            seen_labels.add("booking_ref")

    missing_critical = [key for key in CRITICAL_FIELDS if not values.get(key)]
    missing_critical += [key for key in REQUIRED_LABELS if key not in seen_labels]
    if missing_critical:
        raise SchemaError(
            f"{url}: could not find these fields on the detail page: "
            f"{', '.join(k.replace('_', ' ') for k in missing_critical)}.\n"
            f"  labels we did find: {', '.join(sorted(seen_labels)) or 'none'}\n"
            "These drive the invoice figures, so stopping rather than "
            "producing a row with holes in it."
        )

    missing_optional = [key for key in DETAIL_LABELS if key not in seen_labels]
    for key in missing_optional:
        values[key] = ""
    values["_missing"] = ", ".join(missing_optional)
    return values


def assert_ref_matches(detail: dict[str, str], expected_ref: str, *, url: str) -> None:
    """The detail page must be for the booking we asked for.

    The id is derived arithmetically from the ref, which is an assumption about
    someone else's database. If it ever stops holding we must find out here,
    not by noticing odd numbers on an invoice months later.
    """
    actual = (detail.get("booking_ref") or "").strip()
    if not actual:
        raise DataError(f"{url}: no booking ref on the detail page to check against {expected_ref}")
    if actual != expected_ref:
        raise DataError(
            f"{url}: asked for booking {expected_ref} but the page is for "
            f"{actual}. The rule 'detail id = ref number minus the configured "
            "offset' no longer holds — every figure pulled this way would be "
            "attached to the wrong booking. Stopping."
        )


# ---------------------------------------------------------------------------
# Forms (login)
# ---------------------------------------------------------------------------


@dataclass
class LoginForm:
    action: str
    method: str
    fields: dict[str, str]
    username_field: str
    password_field: str


def find_login_form(html: str, *, url: str) -> LoginForm:
    """Find the login form and everything it wants posted back.

    ASP.NET MVC forms carry a __RequestVerificationToken hidden field that the
    server checks, so we always replay every hidden input we were given.
    """
    soup = soup_of(html)
    for form in soup.find_all("form"):
        password_input = form.find("input", attrs={"type": "password"})
        if not password_input or not password_input.get("name"):
            continue

        fields: dict[str, str] = {}
        username_field = None
        for element in form.find_all(["input", "select", "textarea"]):
            name = element.get("name")
            if not name:
                continue
            input_type = (element.get("type") or "text").lower()
            if input_type in ("submit", "button", "image", "reset"):
                continue
            if input_type in ("checkbox", "radio") and not element.has_attr("checked"):
                continue
            fields[name] = element.get("value", "") or ""
            if input_type in ("text", "email") and username_field is None:
                username_field = name

        if username_field is None:
            raise SchemaError(
                f"{url}: found a login form with a password box but no username "
                f"box. Fields seen: {sorted(fields)}. Set site.username_field "
                "and site.password_field in config.toml."
            )

        return LoginForm(
            action=form.get("action") or url,
            method=(form.get("method") or "post").lower(),
            fields=fields,
            username_field=username_field,
            password_field=password_input["name"],
        )

    raise SchemaError(
        f"{url}: no login form found (no <form> containing a password field). "
        "Set site.login_path in config.toml to the real login URL."
    )


def login_error_message(html: str) -> str | None:
    """Pull the validation summary off a failed login, if there is one."""
    soup = soup_of(html)
    for selector in (
        {"class": "validation-summary-errors"},
        {"class": "field-validation-error"},
        {"class": "alert-danger"},
        {"class": "text-danger"},
    ):
        node = soup.find(attrs=selector)
        if node:
            text = " ".join(node.get_text(" ", strip=True).split())
            if text:
                return text
    return None
