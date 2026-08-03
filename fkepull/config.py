"""Loading config.toml and the .env credentials file."""

from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and older, incl. the one macOS ships
    import tomli as tomllib
from typing import Any

from .errors import ConfigError

DEFAULTS: dict[str, Any] = {
    "site": {
        "base_url": "http://funkidsentertainers.bykayo.digital",
        "completed_path": "/Bookings/MyBookingHistory",
        "upcoming_path": "/Bookings/MyBookings",
        "detail_path": "/Bookings/Details/{id}",
        "login_path": "auto",
        "detail_id_offset": 10000,
        "display_date_format": "auto",
        "search_date_format": "auto",
    },
    "fetch": {
        "max_concurrency": 5,
        "request_delay": 0.0,
        "timeout": 30,
        "retries": 2,
        "user_agent": "fke-pull/1.0 (personal booking export)",
    },
    "app": {
        "port": 8765,
        # Hostnames the app will answer to. Anything else is refused, so a
        # domain someone points at your machine can't reach it. Add your own
        # name here if you set one up in /etc/hosts.
        "allowed_hosts": ["127.0.0.1", "localhost", "::1", "[::1]"],
    },
    "cache": {
        "enabled": True,
        "dir": ".fke-cache",
        "future_ttl_hours": 6,
    },
    "invoice": {
        "percent": 20,
        "rounding": "half_up",
        "total_from_rounded_lines": True,
    },
    "themes": {
        "on_unknown": "warn",
        "fallback": "lowercase",
        "exact": {},
        "rules": [],
    },
}

# The portal will never legitimately need more than this many parallel requests,
# and hammering someone else's server is how you get blocked.
HARD_CONCURRENCY_CAP = 5


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None) -> dict[str, Any]:
    """Read config.toml, merged over the built-in defaults."""
    cfg = DEFAULTS
    if path is not None:
        config_path = Path(path)
        if not config_path.exists():
            raise ConfigError(f"config file not found: {config_path}")
        try:
            with config_path.open("rb") as handle:
                loaded = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc
        cfg = _merge(DEFAULTS, loaded)

    validate_config(cfg)
    return cfg


def validate_config(cfg: dict[str, Any]) -> None:
    site = cfg["site"]
    if not str(site.get("base_url", "")).startswith(("http://", "https://")):
        raise ConfigError("site.base_url must start with http:// or https://")
    if "{id}" not in str(site.get("detail_path", "")):
        raise ConfigError("site.detail_path must contain the {id} placeholder")
    try:
        int(site["detail_id_offset"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError("site.detail_id_offset must be a whole number") from None
    if str(site.get("display_date_format", "auto")).upper() not in ("AUTO", "DMY", "MDY"):
        raise ConfigError('site.display_date_format must be "auto", "DMY" or "MDY"')
    if str(site.get("search_date_format", "auto")).upper() not in ("AUTO", "DMY", "MDY"):
        raise ConfigError('site.search_date_format must be "auto", "DMY" or "MDY"')

    app = cfg["app"]
    try:
        port = int(app["port"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError("app.port must be a whole number") from None
    if not 0 <= port <= 65535:
        raise ConfigError("app.port must be between 0 and 65535")
    if not isinstance(app.get("allowed_hosts"), list) or not app["allowed_hosts"]:
        raise ConfigError("app.allowed_hosts must be a non-empty list of hostnames")

    fetch = cfg["fetch"]
    try:
        concurrency = int(fetch["max_concurrency"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError("fetch.max_concurrency must be a whole number") from None
    if concurrency < 1:
        raise ConfigError("fetch.max_concurrency must be at least 1")

    invoice = cfg["invoice"]
    try:
        percent = int(invoice["percent"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError("invoice.percent must be a whole number") from None
    if not 0 < percent <= 100:
        raise ConfigError("invoice.percent must be between 1 and 100")


def effective_concurrency(cfg: dict[str, Any], override: int | None = None) -> int:
    requested = int(override if override is not None else cfg["fetch"]["max_concurrency"])
    if requested < 1:
        raise ConfigError("concurrency must be at least 1")
    return min(requested, HARD_CONCURRENCY_CAP)


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read a .env file into a dict. Missing file is fine — env vars may be set."""
    env_path = Path(path)
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for lineno, raw in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            raise ConfigError(f"{env_path}:{lineno}: expected NAME=value, got {raw!r}")
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[name.strip()] = value
    return values


def load_credentials(env_file: str | Path) -> tuple[str, str]:
    """Credentials from the environment, falling back to the .env file."""
    from_file = load_env_file(env_file)
    user = os.environ.get("FKE_USER") or from_file.get("FKE_USER")
    password = os.environ.get("FKE_PASS") or from_file.get("FKE_PASS")
    if not user or not password:
        raise ConfigError(
            "FKE_USER and FKE_PASS must be set. Copy .env.example to .env and "
            f"fill it in (looked in the environment and in {env_file})."
        )
    return user, password


def base_url_from(cfg: dict[str, Any], env_file: str | Path, override: str | None) -> str:
    if override:
        return override.rstrip("/")
    from_env = os.environ.get("FKE_BASE_URL") or load_env_file(env_file).get("FKE_BASE_URL")
    if from_env:
        return from_env.rstrip("/")
    return str(cfg["site"]["base_url"]).rstrip("/")
