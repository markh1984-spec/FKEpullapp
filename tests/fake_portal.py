"""A stand-in for the FKE portal, used to test the tool end to end.

It mimics the parts of the real site the tool touches: an ASP.NET MVC login
with an anti-forgery token, the booking history search (which parses DateRange
as US MM/DD/YYYY and quietly returns fewer rows if you send anything else), the
upcoming list, and detail pages — deliberately marked up several different ways
so the field extractor has to cope with more than one layout.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "FAKE-ANTIFORGERY-TOKEN"
COOKIE = "FKEAUTH"

USERNAME = "entertainer@example.com"
PASSWORD = "correct-horse"


@dataclass
class FakeBooking:
    ref: str
    client: str
    package: str
    event_date: str  # DD/MM/YYYY, as a UK site displays it
    postcode: str
    customer: str = "A Customer"
    phone: str = "07700 900000"
    start_time: str = "14:00"
    end_time: str = "15:30"
    balance: str = "£150.00"
    address_1: str = "12 Example Street"
    address_2: str = ""
    town: str = "Leeds"
    county: str = ""
    occasion: str = "Birthday"
    confirmed: str = "01/02/2026"
    notes: str = ""
    entertainer: str = "Mark H"

    @property
    def date(self) -> datetime:
        return datetime.strptime(self.event_date, "%d/%m/%Y")


DEFAULT_BOOKINGS = [
    FakeBooking("100-26501", "Smith", "FUN Kids Party (Indoor Use)", "14/06/2025", "LS1 4AB",
                customer="Jane Smith", phone="07700 900111", balance="£150.00"),
    FakeBooking("100-26502", "Jones", "Outdoor FUN Party", "03/07/2025", "LS2 8BB",
                customer="Tom Jones", start_time="11:00", end_time="12:00", balance="£120.50"),
    FakeBooking("100-26503", "Ali", "FUN School Disco", "21/11/2025", "BD1 1AA",
                customer="Sara Ali", start_time="18:00", end_time="20:00", balance="£240.00"),
    FakeBooking("100-26504", "Brown", "FUN Neon Glow Disco", "05/12/2025", "HX1 2CC",
                customer="Kev Brown", balance="£197.50", occasion="School Christmas"),
    FakeBooking("100-26505", "Patel", "Fun Kids Party + Theme / Spiderman", "09/01/2026", "LS6 3DD",
                customer="Nita Patel", balance="£175.00", address_2="Flat 2"),
    FakeBooking("100-26506", "Green", "Fun Kids Party + Theme / Batman", "14/02/2026", "LS7 4EE",
                customer="Ola Green", balance="£175.00"),
    FakeBooking("100-26507", "White", "FUN Toddler Sensory Session", "02/03/2026", "LS8 5FF",
                customer="Sam White", balance="£99.99", phone=""),
    FakeBooking("100-26528", "Hall", "FUN Kids Party (Indoor Use)", "28/03/2026", "LS9 6GG",
                customer="Ruth Hall", balance="£162.50", start_time="2:00 PM", end_time="4:00 PM"),
    # future — only shows in the upcoming list
    FakeBooking("100-26610", "Future", "FUN School Disco", "12/12/2027", "LS1 1ZZ",
                customer="Later Person", balance="£200.00"),
]


def _rows_html(bookings: list[FakeBooking]) -> str:
    rows = "\n".join(
        f"<tr><td>{b.client}</td><td>{b.package}</td><td>{b.ref}</td>"
        f"<td>{b.event_date}</td><td>{b.postcode}</td></tr>"
        for b in bookings
    )
    if not rows:
        rows = '<tr><td colspan="5">No records found</td></tr>'
    return f"""<!DOCTYPE html><html><head><title>Bookings</title></head><body>
<div class="container">
<h2>Bookings</h2>
<table class="table table-striped">
<thead><tr><th>Client</th><th>Package</th><th>Booking Ref</th><th>Event Date</th><th>Postcode</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</div></body></html>"""


def _detail_html(b: FakeBooking) -> str:
    """Three different label/value layouts on one page, on purpose."""
    return f"""<!DOCTYPE html><html><head><title>Booking Details</title></head><body>
<div class="container">
<h2>Booking Details</h2>

<div class="form-group row">
  <div class="col-md-3"><label class="control-label">Booking Ref</label></div>
  <div class="col-md-9">{b.ref}</div>
</div>
<div class="form-group row">
  <div class="col-md-3"><label class="control-label">Customer</label></div>
  <div class="col-md-9">{b.customer}</div>
</div>
<div class="form-group row">
  <div class="col-md-3"><label class="control-label">Phone</label></div>
  <div class="col-md-9">{b.phone}</div>
</div>

<dl class="dl-horizontal">
  <dt>Entertainer</dt><dd>{b.entertainer}</dd>
  <dt>Package</dt><dd>{b.package}</dd>
  <dt>Event Date</dt><dd>{b.event_date}</dd>
  <dt>Start Time</dt><dd>{b.start_time}</dd>
  <dt>End Time</dt><dd>{b.end_time}</dd>
  <dt>Address Line 1</dt><dd>{b.address_1}</dd>
  <dt>Address Line 2</dt><dd>{b.address_2}</dd>
  <dt>Town/City</dt><dd>{b.town}</dd>
  <dt>County</dt><dd>{b.county}</dd>
  <dt>Postcode</dt><dd>{b.postcode}</dd>
</dl>

<table class="table">
  <tr><th>Balance</th><td>{b.balance}</td></tr>
  <tr><th>Party Occasion</th><td>{b.occasion}</td></tr>
  <tr><th>Booking Confirmed Date</th><td>{b.confirmed}</td></tr>
</table>

<div class="display-label">Internal Notes</div><div class="display-field">{b.notes}</div>
<div class="display-label">Parking/Location Information</div><div class="display-field"></div>
<div class="display-label">Playlist</div><div class="display-field"></div>
<div class="display-label">Other Party Notes</div><div class="display-field"></div>
</div></body></html>"""


LOGIN_HTML = f"""<!DOCTYPE html><html><head><title>Log in</title></head><body>
<h2>Log in</h2>
{{error}}
<form action="/Account/Login" method="post">
<input name="__RequestVerificationToken" type="hidden" value="{TOKEN}" />
<input name="ReturnUrl" type="hidden" value="/" />
<div><label for="UserName">Email</label><input id="UserName" name="UserName" type="text" value="" /></div>
<div><label for="Password">Password</label><input id="Password" name="Password" type="password" /></div>
<div><input name="RememberMe" type="checkbox" value="true" /></div>
<input type="submit" value="Log in" />
</form></body></html>"""


@dataclass
class PortalState:
    bookings: list[FakeBooking] = field(
        default_factory=lambda: [replace(b) for b in DEFAULT_BOOKINGS]
    )
    today: datetime = field(default_factory=lambda: datetime(2026, 6, 1))
    requests: list[str] = field(default_factory=list)
    date_ranges: list[str] = field(default_factory=list)
    detail_hits: dict[str, int] = field(default_factory=dict)
    max_concurrent: int = 0
    _in_flight: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    detail_delay: float = 0.05
    break_detail_layout: bool = False
    # Serve each detail page under someone else's booking ref, i.e. pretend the
    # "detail id = ref number minus 10000" rule has stopped holding.
    mislabel_details: bool = False

    def enter(self) -> None:
        with self._lock:
            self._in_flight += 1
            self.max_concurrent = max(self.max_concurrent, self._in_flight)

    def leave(self) -> None:
        with self._lock:
            self._in_flight -= 1

    def record(self, line: str) -> None:
        with self._lock:
            self.requests.append(line)


def _handler_class(state: PortalState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # keep the test output quiet
            pass

        # -- helpers ---------------------------------------------------
        def _authed(self) -> bool:
            return COOKIE in (self.headers.get("Cookie") or "")

        def _send(self, body: str, status: int = 200, headers: dict | None = None) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def _redirect(self, location: str, headers: dict | None = None) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()

        def _login_required(self) -> None:
            self._redirect("/Account/Login?ReturnUrl=%2F")

        # -- routes ----------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            state.record(f"GET {path}")
            state.enter()
            try:
                if path == "/Account/Login":
                    self._send(LOGIN_HTML.format(error=""))
                elif not self._authed():
                    self._login_required()
                elif path == "/":
                    self._send("<html><body><h1>Dashboard</h1></body></html>")
                elif path == "/Bookings/MyBookings":
                    upcoming = [b for b in state.bookings if b.date > state.today]
                    self._send(_rows_html(upcoming))
                elif path.startswith("/Bookings/Details/"):
                    self._detail(path)
                else:
                    self._send("<html><body>Not found</body></html>", status=404)
            finally:
                state.leave()

        def _detail(self, path: str) -> None:
            import time as _time

            if state.detail_delay:
                _time.sleep(state.detail_delay)
            try:
                detail_id = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self._send("<html><body>Bad id</body></html>", status=404)
                return
            for booking in state.bookings:
                if int(booking.ref.split("-")[1]) - 10000 == detail_id:
                    state.detail_hits[booking.ref] = state.detail_hits.get(booking.ref, 0) + 1
                    if state.mislabel_details:
                        prefix, number = booking.ref.split("-")
                        booking = replace(booking, ref=f"{prefix}-{int(number) + 1}")
                    html = _detail_html(booking)
                    if state.break_detail_layout:
                        html = html.replace("Balance", "Amount Due")
                    self._send(html)
                    return
            self._send("<html><body>Not found</body></html>", status=404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            state.record(f"POST {path}")
            state.enter()
            try:
                if path == "/Account/Login":
                    self._login(form)
                elif not self._authed():
                    self._login_required()
                elif path == "/Bookings/MyBookingHistory":
                    self._history(form)
                else:
                    self._send("<html><body>Not found</body></html>", status=404)
            finally:
                state.leave()

        def _login(self, form: dict) -> None:
            if form.get("__RequestVerificationToken", [""])[0] != TOKEN:
                self._send(
                    LOGIN_HTML.format(
                        error='<div class="validation-summary-errors">Invalid request.</div>'
                    ),
                    status=200,
                )
                return
            user = form.get("UserName", [""])[0]
            password = form.get("Password", [""])[0]
            if user != USERNAME or password != PASSWORD:
                self._send(
                    LOGIN_HTML.format(
                        error='<div class="validation-summary-errors">'
                              "Invalid login attempt.</div>"
                    )
                )
                return
            self._redirect("/", headers={"Set-Cookie": f"{COOKIE}=1; Path=/; HttpOnly"})

        def _history(self, form: dict) -> None:
            raw_range = form.get("DateRange", [""])[0]
            state.date_ranges.append(raw_range)

            start, end = None, None
            parts = [part.strip() for part in raw_range.split("-")]
            if len(parts) == 2:
                # The real site reads this as US MM/DD/YYYY. Anything it can't
                # read that way matches nothing — it does not complain.
                try:
                    start = datetime.strptime(parts[0], "%m/%d/%Y")
                    end = datetime.strptime(parts[1], "%m/%d/%Y")
                except ValueError:
                    start = end = None

            if start is None or end is None:
                self._send(_rows_html([]))
                return

            done = [
                b for b in state.bookings
                if b.date <= state.today and start <= b.date <= end
            ]
            self._send(_rows_html(sorted(done, key=lambda b: b.date)))

    return Handler


class FakePortal:
    """Context manager that runs the fake portal on a random local port."""

    def __init__(self, state: PortalState | None = None) -> None:
        self.state = state or PortalState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_class(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakePortal":
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
