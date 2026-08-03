"""HTTP client for the FKE portal.

Holds the session cookie, logs in, posts the booking search, and fetches detail
pages — never more than `max_concurrency` requests at a time.
"""

from __future__ import annotations

import threading
import time as time_module
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .cache import DetailCache
from .dates import format_search_range
from .errors import DataError, FKEError, LoginError
from .parse import BOOKING_REF_RE, find_login_form, login_error_message, looks_like_login_page

DEFAULT_LOGIN_CANDIDATES = ("/Account/Login", "/Login", "/Account/LogOn", "/Home/Login")


def detail_id_for(ref: str, offset: int) -> int:
    """Detail page id for a booking ref: numeric part minus the offset.

    e.g. "100-26528" with offset 10000 -> 16528. This is an assumption about
    someone else's id scheme; parse.assert_ref_matches checks it held.
    """
    match = BOOKING_REF_RE.search(ref or "")
    if not match:
        raise DataError(f"{ref!r} is not a booking ref we recognise (expected e.g. 100-26528)")
    number = int(match.group(2))
    detail_id = number - offset
    if detail_id <= 0:
        raise DataError(
            f"booking ref {ref} gives detail id {detail_id} with offset {offset} — "
            "that can't be right. Check site.detail_id_offset in config.toml."
        )
    return detail_id


class _Throttle:
    """Caps in-flight requests and, optionally, how fast we start new ones."""

    def __init__(self, max_concurrency: int, delay: float = 0.0) -> None:
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self._delay = max(0.0, float(delay))
        self._lock = threading.Lock()
        self._last_start = 0.0
        self.max_observed = 0
        self._in_flight = 0
        self._counter_lock = threading.Lock()

    def __enter__(self) -> "_Throttle":
        self._semaphore.acquire()
        with self._counter_lock:
            self._in_flight += 1
            self.max_observed = max(self.max_observed, self._in_flight)
        if self._delay:
            with self._lock:
                wait = self._last_start + self._delay - time_module.monotonic()
                if wait > 0:
                    time_module.sleep(wait)
                self._last_start = time_module.monotonic()
        return self

    def __exit__(self, *exc_info: object) -> None:
        with self._counter_lock:
            self._in_flight -= 1
        self._semaphore.release()


@dataclass
class FKEClient:
    base_url: str
    username: str
    password: str
    timeout: int = 30
    retries: int = 2
    max_concurrency: int = 5
    request_delay: float = 0.0
    user_agent: str = "fke-pull/1.0"
    login_path: str = "auto"
    username_field: str | None = None
    password_field: str | None = None
    verbose: bool = False
    log: callable = print

    _cookies: dict = field(default_factory=dict, init=False)
    _local: threading.local = field(default_factory=threading.local, init=False)
    _logged_in: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.throttle = _Throttle(self.max_concurrency, self.request_delay)
        self._main_session = self._new_session()

    # -- plumbing ----------------------------------------------------------

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": self.user_agent})
        retry = Retry(
            total=self.retries,
            connect=self.retries,
            read=self.retries,
            status=self.retries,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            backoff_factor=1.0,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=self.max_concurrency)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _session(self) -> requests.Session:
        """A session for the calling thread, sharing the login cookies.

        requests' cookie jar isn't safe to mutate from several threads at once,
        so each worker thread gets its own session seeded with the cookies we
        got at login.
        """
        session = getattr(self._local, "session", None)
        if session is None:
            if threading.current_thread() is threading.main_thread():
                session = self._main_session
            else:
                session = self._new_session()
                session.cookies.update(self._cookies)
            self._local.session = session
        return session

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        session = self._session()
        with self.throttle:
            if self.verbose:
                self.log(f"  {method} {url}")
            try:
                response = session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                raise FKEError(f"{method} {url} failed: {exc}") from exc
        if response.status_code >= 400:
            raise FKEError(
                f"{method} {url} returned HTTP {response.status_code}. "
                + (
                    "That usually means the session expired or the path has moved."
                    if response.status_code in (401, 403, 404)
                    else "The portal may be having trouble; try again shortly."
                )
            )
        return response

    def get(self, path: str, **kwargs) -> requests.Response:
        return self._request("GET", self._url(path), **kwargs)

    def post(self, path: str, data: dict, **kwargs) -> requests.Response:
        return self._request("POST", self._url(path), data=data, **kwargs)

    # -- login -------------------------------------------------------------

    def _login_page(self) -> requests.Response:
        if self.login_path and self.login_path != "auto":
            return self.get(self.login_path)

        # An [Authorize]-protected home page redirects us to wherever login is.
        response = self.get("/", allow_redirects=True)
        if not looks_like_login_page(response.text):
            for candidate in DEFAULT_LOGIN_CANDIDATES:
                try:
                    attempt = self.get(candidate)
                except FKEError:
                    continue
                if looks_like_login_page(attempt.text):
                    return attempt
            raise LoginError(
                f"Couldn't find the login page. {self.base_url}/ didn't redirect to "
                f"one and none of {', '.join(DEFAULT_LOGIN_CANDIDATES)} had a "
                "password field. Set site.login_path in config.toml."
            )
        return response

    def login(self) -> None:
        page = self._login_page()
        form = find_login_form(page.text, url=page.url)

        user_field = self.username_field or form.username_field
        pass_field = self.password_field or form.password_field
        payload = dict(form.fields)  # replays __RequestVerificationToken etc.
        payload[user_field] = self.username
        payload[pass_field] = self.password

        action = urljoin(page.url, form.action)
        if self.verbose:
            hidden = [k for k in form.fields if k not in (user_field, pass_field)]
            self.log(f"  login form at {action} (also posting: {', '.join(hidden) or 'nothing'})")

        response = self._request(
            "POST", action, data=payload, headers={"Referer": page.url}, allow_redirects=True
        )

        if looks_like_login_page(response.text):
            reason = login_error_message(response.text)
            raise LoginError(
                "Login was rejected"
                + (f": {reason}" if reason else " (still on the login page afterwards)")
                + ". Check FKE_USER / FKE_PASS in .env."
            )

        self._cookies = requests.utils.dict_from_cookiejar(self._main_session.cookies)
        if not self._cookies:
            raise LoginError(
                "Login appeared to succeed but the portal set no session cookie, "
                "so nothing else will work. The login flow has probably changed."
            )
        self._logged_in = True
        if self.verbose:
            self.log(f"  logged in, session cookie(s): {', '.join(sorted(self._cookies))}")

    def _require_login(self) -> None:
        if not self._logged_in:
            raise LoginError("internal error: tried to fetch bookings before logging in")

    # -- bookings ----------------------------------------------------------

    def fetch_completed_html(
        self, path: str, start: date, end: date, search_format: str = "MDY"
    ) -> tuple[str, str]:
        """POST the booking history search. Returns (html, url).

        `search_format` is the order the DateRange field wants — "MDY" or "DMY".
        The caller works that out by trying both; see cli._fetch_completed. The
        other three filters are sent empty so they match everything.
        """
        self._require_login()
        payload = {
            "DateRange": format_search_range(start, end, search_format),
            "ClientID": "",
            "PackageID": "",
            "BookingRef": "",
        }
        url = self._url(path)
        if self.verbose:
            self.log(f"  searching {payload['DateRange']} ({search_format})")
        response = self._request("POST", url, data=payload, headers={"Referer": url})
        return response.text, response.url

    def fetch_upcoming_html(self, path: str) -> tuple[str, str]:
        self._require_login()
        response = self.get(path)
        return response.text, response.url

    def fetch_detail_html(self, ref: str, *, detail_path: str, offset: int) -> tuple[str, str]:
        self._require_login()
        path = detail_path.format(id=detail_id_for(ref, offset))
        response = self.get(path)
        return response.text, response.url

    def fetch_details(
        self,
        refs: list[str],
        *,
        detail_path: str,
        offset: int,
        cache: DetailCache,
        event_dates: dict[str, date | None],
        on_progress: callable | None = None,
    ) -> dict[str, tuple[str, str]]:
        """Fetch every detail page, cache-first, at most max_concurrency at once."""
        self._require_login()
        results: dict[str, tuple[str, str]] = {}
        errors: list[str] = []
        lock = threading.Lock()

        def work(ref: str) -> None:
            event_date = event_dates.get(ref)
            url = self._url(detail_path.format(id=detail_id_for(ref, offset)))
            cached = cache.get(ref, event_date=event_date)
            if cached is not None:
                with lock:
                    results[ref] = (cached, url + " (cached)")
                    if on_progress:
                        on_progress(ref, True)
                return
            try:
                html, actual_url = self.fetch_detail_html(ref, detail_path=detail_path, offset=offset)
            except FKEError as exc:
                with lock:
                    errors.append(f"{ref}: {exc}")
                return
            cache.put(ref, html, url=actual_url, event_date=event_date)
            with lock:
                results[ref] = (html, actual_url)
                if on_progress:
                    on_progress(ref, False)

        with ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
            list(pool.map(work, refs))

        if errors:
            raise FKEError(
                "Failed to fetch "
                f"{len(errors)} of {len(refs)} detail page(s):\n  "
                + "\n  ".join(errors[:10])
                + ("\n  ..." if len(errors) > 10 else "")
            )
        return results
