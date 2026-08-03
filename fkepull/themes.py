"""Turning package names into the short theme labels used on the invoice."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import ConfigError


def normalise_package(name: str) -> str:
    """Case- and whitespace-insensitive key for matching package names."""
    return " ".join((name or "").split()).lower()


@dataclass
class ThemeRule:
    pattern: re.Pattern[str]
    template: str


@dataclass
class ThemeMapper:
    exact: dict[str, str]
    rules: list[ThemeRule]
    on_unknown: str = "warn"
    fallback: str = "lowercase"
    unknown: set[str] = field(default_factory=set)

    @classmethod
    def from_config(cls, cfg: dict) -> "ThemeMapper":
        section = cfg.get("themes", {}) or {}

        exact_raw = section.get("exact", {}) or {}
        if not isinstance(exact_raw, dict):
            raise ConfigError("[themes.exact] in config.toml must be a table of package name = theme")
        exact = {normalise_package(k): str(v).strip() for k, v in exact_raw.items()}

        rules: list[ThemeRule] = []
        for index, entry in enumerate(section.get("rules", []) or []):
            if not isinstance(entry, dict) or "pattern" not in entry or "theme" not in entry:
                raise ConfigError(
                    f"[[themes.rules]] entry {index + 1} in config.toml needs both "
                    "a `pattern` and a `theme`"
                )
            try:
                compiled = re.compile(entry["pattern"], re.IGNORECASE)
            except re.error as exc:
                raise ConfigError(
                    f"[[themes.rules]] entry {index + 1}: {entry['pattern']!r} "
                    f"is not a valid regular expression: {exc}"
                ) from exc
            rules.append(ThemeRule(compiled, str(entry["theme"])))

        on_unknown = str(section.get("on_unknown", "warn")).lower()
        if on_unknown not in ("warn", "fail"):
            raise ConfigError("themes.on_unknown must be either \"warn\" or \"fail\"")

        fallback = str(section.get("fallback", "lowercase")).lower()
        if fallback not in ("lowercase", "empty"):
            raise ConfigError("themes.fallback must be either \"lowercase\" or \"empty\"")

        return cls(exact=exact, rules=rules, on_unknown=on_unknown, fallback=fallback)

    def theme_for(self, package: str) -> str:
        key = normalise_package(package)
        if not key:
            return ""

        if key in self.exact:
            return self.exact[key]

        for rule in self.rules:
            match = rule.pattern.search(package)
            if not match:
                continue
            try:
                value = rule.template.format(**match.groupdict())
            except (KeyError, IndexError) as exc:
                raise ConfigError(
                    f"theme rule {rule.pattern.pattern!r} refers to a capture group "
                    f"that isn't in the pattern: {exc}"
                ) from exc
            return " ".join(value.split()).lower()

        self.unknown.add(package.strip())
        return key if self.fallback == "lowercase" else ""
