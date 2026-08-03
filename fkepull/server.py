"""The local web app: past and upcoming bookings in a browser.

Serves on 127.0.0.1 only, so nothing outside this machine can reach it. Every
request also has to carry the token in the launch URL, because any web page you
happen to have open can otherwise make requests to localhost. That token is kept
between runs so the link can be bookmarked.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .dates import format_duration, format_uk
from .errors import FKEError
from .money import format_money
from .pipeline import PullOptions, PullResult, pull
from .report import Booking, csv_text, details_table, invoice_table

PAGE = Path(__file__).resolve().parent / "webapp.html"
TOKEN_FILE = "app-token.txt"


def resolve_token(cache_dir: Path) -> str:
    """The app's access token, kept the same between runs so you can bookmark it.

    Deliberately not a cookie: cookies aren't isolated by port, so any other
    local web app you visit would be handed this one's credentials by the
    browser. Keeping it in the URL means only whoever has the link can get in.

    Delete the file to rotate it, or set FKE_APP_TOKEN to pin your own.
    """
    from_env = os.environ.get("FKE_APP_TOKEN")
    if from_env:
        return from_env

    path = Path(cache_dir) / TOKEN_FILE
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass

    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        pass  # can't remember it; a fresh token each run still works
    return token


def _row(booking: Booking, *, percent: int, rounding: str) -> dict:
    cut = booking.commission(percent=percent, rounding=rounding)
    minutes = booking.duration_minutes
    return {
        "ref": booking.ref,
        "date": format_uk(booking.event_date),
        "sort_date": booking.event_date.isoformat(),
        "customer": booking.detail.get("customer", ""),
        "phone": booking.detail.get("phone", ""),
        "theme": booking.theme,
        "package": booking.package,
        "venue": booking.detail.get("address_line_1", ""),
        "occasion": booking.detail.get("party_occasion", ""),
        "confirmed": booking.detail.get("confirmed_date", ""),
        "duration": format_duration(minutes) if minutes is not None else "",
        "fee": format_money(booking.fee) if booking.fee is not None else "",
        "cut": format_money(cut) if booking.fee is not None else "",
        "invoiced": booking.invoiced,
    }


def _section(bookings: list[Booking], *, percent: int, rounding: str) -> dict:
    rows = [_row(b, percent=percent, rounding=rounding) for b in bookings]
    fee_total = sum((b.fee or Decimal(0) for b in bookings), Decimal(0))
    cut_total = sum(
        (b.commission(percent=percent, rounding=rounding) for b in bookings), Decimal(0)
    )
    return {
        "rows": rows,
        "count": len(rows),
        "total_fee": format_money(fee_total),
        "total_cut": format_money(cut_total),
    }


@dataclass
class AppState:
    """Holds the most recent pull, and knows how to redo it."""

    make_context: callable
    options: PullOptions
    cfg: dict
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1", "[::1]")

    result: PullResult | None = None
    error: str | None = None
    log_lines: list[str] = field(default_factory=list)
    pulled_at: str | None = None
    busy: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def refresh(self, *, hard: bool = False) -> None:
        with self._lock:
            self.busy = True
            self.log_lines = []
            try:
                context = self.make_context(refresh_cache=hard, log_fn=self.log_lines.append)
                self.result = pull(context, self.options, self.log_lines.append)
                self.error = None
                self.pulled_at = datetime.now().strftime("%H:%M on %d/%m/%Y")
            except FKEError as exc:
                # Keep serving whatever we last had; the page shows the problem.
                self.error = str(exc)
            except Exception as exc:  # unexpected — still don't kill the server
                self.error = f"{type(exc).__name__}: {exc}"
            finally:
                self.busy = False

    # -- what the page asks for -------------------------------------------

    def snapshot(self) -> dict:
        percent = int(self.cfg["invoice"]["percent"])
        rounding = str(self.cfg["invoice"]["rounding"])
        result = self.result
        empty = {"rows": [], "count": 0, "total_fee": "0.00", "total_cut": "0.00"}
        return {
            "percent": percent,
            "pulled_at": self.pulled_at,
            "error": self.error,
            "past": _section(result.past, percent=percent, rounding=rounding) if result else empty,
            "upcoming": (
                _section(result.upcoming, percent=percent, rounding=rounding) if result else empty
            ),
            "warnings": list(result.warnings) if result else [],
            "unknown_packages": list(result.unknown_packages) if result else [],
            "skipped_invoiced": list(result.skipped_invoiced) if result else [],
            "missing_refs": list(result.missing_refs) if result else [],
            "log": self.log_lines,
        }

    def csv_for(self, name: str) -> str | None:
        if self.result is None:
            return None
        percent = int(self.cfg["invoice"]["percent"])
        rounding = str(self.cfg["invoice"]["rounding"])
        totals = bool(self.cfg["invoice"]["total_from_rounded_lines"])
        mark = self.options.invoiced_mode == "mark" and bool(self.options.invoiced_refs)

        if name == "invoice":
            return csv_text(*invoice_table(
                self.result.past, percent=percent, rounding=rounding,
                total_from_rounded_lines=totals, mark_invoiced=mark,
            ))
        if name == "upcoming":
            return csv_text(*invoice_table(
                self.result.upcoming, percent=percent, rounding=rounding,
                total_from_rounded_lines=totals, mark_invoiced=mark,
            ))
        if name == "details":
            return csv_text(*details_table(self.result.bookings))
        return None


NO_TOKEN_PAGE = """<!DOCTYPE html><html lang="en-GB"><head><meta charset="utf-8">
<title>FKE bookings</title><style>
body{font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
max-width:34rem;margin:16vh auto;padding:0 1.5rem;color:#1c1e21}
code{background:#f0f2f5;padding:2px 6px;border-radius:4px}
@media(prefers-color-scheme:dark){body{background:#14161a;color:#e7e9ec}
code{background:#242830}}</style></head><body>
<h1>Not this link</h1>
<p>This app needs the full link, the one with <code>?t=…</code> on the end.</p>
<p>It is printed in the Terminal window when the app starts. Bookmark that one
and it will keep working — the link stays the same between runs.</p>
</body></html>"""


def _handler_class(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "fke-pull"

        def log_message(self, *args) -> None:
            pass  # the tool does its own logging

        # -- guards --------------------------------------------------------

        def _local_host(self) -> bool:
            raw = self.headers.get("Host") or ""
            host = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
            return host.lower() in state.allowed_hosts

        def _token_ok(self, query: dict) -> bool:
            supplied = self.headers.get("X-FKE-Token") or (query.get("t") or [""])[0]
            return secrets.compare_digest(supplied, state.token)

        # -- replies -------------------------------------------------------

        def _send(self, body: bytes, content_type: str, status: int = 200, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, status: int = 200) -> None:
            self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status)

        def _text(self, message: str, status: int) -> None:
            self._send(message.encode("utf-8"), "text/plain; charset=utf-8", status)

        # -- routes --------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            if not self._local_host():
                self._text(
                    "This app only answers to "
                    f"{', '.join(state.allowed_hosts)}. If you want to reach it "
                    "by another name, add that name to app.allowed_hosts in "
                    "config.toml.",
                    403,
                )
                return
            if not self._token_ok(query):
                self._send(
                    NO_TOKEN_PAGE.encode("utf-8"), "text/html; charset=utf-8", 403
                )
                return

            if parsed.path in ("/", "/index.html"):
                try:
                    html = PAGE.read_text(encoding="utf-8")
                except OSError as exc:
                    self._text(f"Couldn't read {PAGE}: {exc}", 500)
                    return
                html = html.replace("__FKE_TOKEN__", state.token)
                self._send(html.encode("utf-8"), "text/html; charset=utf-8")
                return

            if parsed.path == "/api/data":
                self._json(state.snapshot())
                return

            if parsed.path.startswith("/download/"):
                name = parsed.path[len("/download/"):].removesuffix(".csv")
                body = state.csv_for(name)
                if body is None:
                    self._text("Nothing to download yet.", 404)
                    return
                self._send(
                    body.encode("utf-8-sig"),
                    "text/csv; charset=utf-8",
                    extra={"Content-Disposition": f'attachment; filename="{name}.csv"'},
                )
                return

            self._text("Not found", 404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)

            if not self._local_host() or not self._token_ok(query):
                self._text("Forbidden", 403)
                return

            if parsed.path == "/api/refresh":
                state.refresh(hard=(query.get("hard") or [""])[0] == "1")
                self._json(state.snapshot())
                return

            self._text("Not found", 404)

    return Handler


def create_server(args, cfg: dict, options: PullOptions, *, port: int, log=print):
    """Build the server without starting it — used by --serve and by the tests."""
    from .cli import context_from

    def make_context(refresh_cache: bool = False, log_fn=None):
        # A fresh context per pull: new login, new session, so a browser sitting
        # open for hours doesn't run into an expired cookie.
        context = context_from(args, cfg, log_fn=log_fn or (lambda message: None))
        if refresh_cache:
            context.cache.refresh = True
        return context

    cache_dir = args.cache_dir or cfg["cache"]["dir"]
    state = AppState(
        make_context=make_context,
        options=options,
        cfg=cfg,
        token=resolve_token(Path(cache_dir)),
        allowed_hosts=tuple(str(h).lower() for h in cfg["app"]["allowed_hosts"]),
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler_class(state))
    host, actual_port = server.server_address[:2]
    url = f"http://{host}:{actual_port}/?t={state.token}"
    return server, state, url


def serve(args, cfg: dict, options: PullOptions, *, port: int, open_browser: bool, log=print) -> int:
    server, state, url = create_server(args, cfg, options, port=port, log=log)

    log("Pulling your bookings…")
    state.refresh()
    for line in state.log_lines:
        log(line)
    if state.error:
        log("")
        log(f"ERROR: {state.error}")
        log("Starting anyway — fix the problem and press Refresh in the browser.")

    log("")
    log(f"fke-pull is running at {url}")
    log("Bookmark that link — it stays the same next time.")
    log("Only this machine can reach it. Press Ctrl-C to stop.")

    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("")
        log("Stopped.")
    finally:
        server.server_close()
    return 0
