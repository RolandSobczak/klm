"""User configuration — `config.toml` in the catalog directory.

Everything here has a working default, so klm runs correctly with no config
file at all. The file exists for the two things klm genuinely cannot know:
which non-canonical fields the user considers legitimate, and which historical
spellings their own library uses (docs/05 §1 and §2).

User aliases *extend* the built-in map rather than replacing it. Replacing it
would mean adding one local spelling silently switched off the other forty.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from klm import fields

__all__ = ["Config", "ConfigError", "FieldConfig", "LintConfig", "load_config"]

DEFAULT_MAX_SEVERITY = "error"


class ConfigError(Exception):
    """The configuration file exists but cannot be used."""


@dataclass(frozen=True)
class FieldConfig:
    """The field schema as this catalog sees it."""

    aliases: dict[str, str] = field(default_factory=dict)
    """Folded alias key → canonical field name."""
    custom: dict[str, str] = field(default_factory=dict)
    """Field name → why it exists. Declared fields are not flagged as unknown."""

    def canonical(self, name: str) -> str | None:
        """The canonical spelling of a field, or ``None`` if klm doesn't know it.

        A name that is already canonical returns itself, so callers can treat
        this as "resolve" rather than "translate".
        """
        key = fields.alias_key(name)
        for known in (*fields.KNOWN_FIELDS, *self.custom):
            if fields.alias_key(known) == key:
                return known
        return self.aliases.get(key)

    def is_declared(self, name: str) -> bool:
        """True when the user has claimed this field in `[fields.custom]`."""
        key = fields.alias_key(name)
        return any(fields.alias_key(declared) == key for declared in self.custom)


@dataclass(frozen=True)
class LintConfig:
    select: tuple[str, ...] = ()
    """Rule IDs or group letters to run. Empty means every rule."""
    ignore: tuple[str, ...] = ()
    max_severity: str = DEFAULT_MAX_SEVERITY
    """Lowest severity that makes `klm lint` exit non-zero."""


@dataclass(frozen=True)
class Config:
    fields: FieldConfig = field(default_factory=FieldConfig)
    lint: LintConfig = field(default_factory=LintConfig)
    significant_figures: int = 3
    """Significant figures in formatted values."""
    source: Path | None = None
    """Where this came from, for error messages. ``None`` means defaults only."""


def default_aliases() -> dict[str, str]:
    """The built-in alias map, folded to lookup keys."""
    table: dict[str, str] = {}
    for canonical, spellings in fields.DEFAULT_ALIASES.items():
        for spelling in spellings:
            table[fields.alias_key(spelling)] = canonical
    return table


def load_config(path: Path | None) -> Config:
    """Read `config.toml`, or return defaults when it is absent.

    A missing file is normal. A malformed one is not: klm raises rather than
    falling back, because silently ignoring a config means linting against
    rules the user believes they changed.
    """
    aliases = default_aliases()
    if path is None or not path.exists():
        return Config(fields=FieldConfig(aliases=aliases))

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    field_section = _table(data, "fields", path)
    for canonical, spellings in _table(field_section, "aliases", path).items():
        if not isinstance(spellings, list) or not all(isinstance(s, str) for s in spellings):
            raise ConfigError(f"{path}: [fields.aliases] {canonical} must be a list of strings")
        for spelling in spellings:
            aliases[fields.alias_key(spelling)] = canonical

    custom = {
        name: str(reason) for name, reason in _table(field_section, "custom", path).items()
    }
    for name in custom:
        if fields.is_klm_reserved(name):
            raise ConfigError(f"{path}: '{name}' is in klm's reserved KLM_ namespace")

    lint_section = _table(data, "lint", path)
    lint = LintConfig(
        select=_string_tuple(lint_section, "select", path),
        ignore=_string_tuple(lint_section, "ignore", path),
        max_severity=str(lint_section.get("max_severity", DEFAULT_MAX_SEVERITY)),
    )
    if lint.max_severity not in ("error", "warning"):
        raise ConfigError(f"{path}: max_severity must be 'error' or 'warning'")

    format_section = _table(data, "format", path)
    significant = format_section.get("significant_figures", 3)
    if not isinstance(significant, int) or not 1 <= significant <= 6:
        raise ConfigError(f"{path}: significant_figures must be an integer between 1 and 6")

    return Config(
        fields=FieldConfig(aliases=aliases, custom=custom),
        lint=lint,
        significant_figures=significant,
        source=path,
    )


def _table(data: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: [{key}] must be a table")
    return value


def _string_tuple(data: dict[str, Any], key: str, path: Path) -> tuple[str, ...]:
    value = data.get(key, [])
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{path}: {key} must be a list of strings")
    return tuple(value)
