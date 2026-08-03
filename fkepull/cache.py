"""On-disk cache of fetched detail pages.

A party that has already happened can't change, so its detail page is cached
forever. A booking still in the future can change (times, address, balance), so
those are only cached briefly. That way repeat runs barely touch FKE's server
without ever serving stale numbers for upcoming work.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class DetailCache:
    directory: Path
    enabled: bool = True
    future_ttl_hours: float = 6.0
    refresh: bool = False

    hits: int = 0
    misses: int = 0
    writes: int = 0

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)

    def _path(self, ref: str) -> Path:
        return self.directory / "details" / f"{_SAFE.sub('_', ref)}.json"

    def get(self, ref: str, *, event_date: date | None, today: date | None = None) -> str | None:
        if not self.enabled or self.refresh:
            return None
        path = self._path(ref)
        if not path.exists():
            self.misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            html = payload["html"]
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
        except (ValueError, KeyError, OSError):
            # A corrupt cache entry is never a reason to fail a run.
            self.misses += 1
            return None

        today = today or date.today()
        if event_date is None or event_date >= today:
            if datetime.now() - fetched_at > timedelta(hours=self.future_ttl_hours):
                self.misses += 1
                return None

        self.hits += 1
        return html

    def put(self, ref: str, html: str, *, url: str, event_date: date | None) -> None:
        if not self.enabled:
            return
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ref": ref,
            "url": url,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "event_date": event_date.isoformat() if event_date else None,
            "html": html,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
        self.writes += 1

    # -- remembering what we learned about the portal ----------------------

    @property
    def _portal_file(self) -> Path:
        return self.directory / "portal.json"

    def remembered_search_format(self) -> str | None:
        """The DateRange order that worked last time, if we've proved one."""
        if not self.enabled or self.refresh:
            return None
        try:
            value = json.loads(self._portal_file.read_text(encoding="utf-8"))["search_date_format"]
        except (OSError, ValueError, KeyError):
            return None
        return value if value in ("MDY", "DMY") else None

    def remember_search_format(self, value: str) -> None:
        if not self.enabled:
            return
        self._portal_file.parent.mkdir(parents=True, exist_ok=True)
        self._portal_file.write_text(
            json.dumps(
                {
                    "search_date_format": value,
                    "detected_at": datetime.now().isoformat(timespec="seconds"),
                }
            ),
            encoding="utf-8",
        )

    def summary(self) -> str:
        if not self.enabled:
            return "cache disabled"
        return f"cache: {self.hits} hit(s), {self.misses} miss(es), {self.writes} written"
