"""User configuration — `config.toml` in the catalog directory.

Everything here has a working default, so klm runs correctly with no config
file at all. The file exists for the two things klm genuinely cannot know:
which non-canonical fields the user considers legitimate, and which historical
spellings their own library uses (docs/05 §1 and §2).

User aliases *extend* the built-in map rather than replacing it. Replacing it
would mean adding one local spelling silently switched off the other forty.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from klm import fields

__all__ = [
    "Config",
    "ConfigError",
    "FieldConfig",
    "LintConfig",
    "SupplierConfig",
    "load_config",
    "resolve_secret",
]

DEFAULT_MAX_SEVERITY = "error"
DEFAULT_STALE_DAYS = 30
ENV_PREFIX = "env:"


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
class SupplierConfig:
    """One `[suppliers.<name>]` block (docs/07 §7).

    Credentials are *references* to environment variables, never the secrets
    themselves. `config.toml` lives in a directory users are encouraged to put
    under git; a file format that invites pasting an API key into it is a file
    format that leaks API keys.
    """

    name: str
    enabled: bool = False
    mode: str = "api"
    """`api` | `manual`. `manual` means klm never calls out for this supplier."""
    api_key_ref: str | None = None
    api_secret_ref: str | None = None
    currency: str = "PLN"
    rate_per_second: float = 2.0
    """Deliberately conservative. Being throttled is a self-inflicted outage."""
    burst: float = 4.0
    shipping_flat: float = 0.0
    free_shipping_above: float | None = None
    vat_rate: float = 0.0
    """VAT added to the quoted prices. Set it the same way on every supplier, or
    the landed-cost comparison tilts toward whichever one it was omitted from."""
    import_charges: bool = False
    """Whether customs duty applies — an estimate klm labels as one."""

    @property
    def manual(self) -> bool:
        return self.mode == "manual"

    def credentials(self) -> tuple[str | None, str | None]:
        """Resolve the key and secret from the environment, at the point of use."""
        return resolve_secret(self.api_key_ref), resolve_secret(self.api_secret_ref)

    def has_credentials(self) -> bool:
        key, secret = self.credentials()
        return bool(key and secret)


@dataclass(frozen=True)
class Config:
    fields: FieldConfig = field(default_factory=FieldConfig)
    lint: LintConfig = field(default_factory=LintConfig)
    suppliers: dict[str, SupplierConfig] = field(default_factory=dict)
    stale_days: int = DEFAULT_STALE_DAYS
    """Age past which `klm lint` calls an offer stale (P003)."""
    significant_figures: int = 3
    """Significant figures in formatted values."""
    source: Path | None = None
    """Where this came from, for error messages. ``None`` means defaults only."""

    def enabled_suppliers(self) -> list[SupplierConfig]:
        return [s for s in self.suppliers.values() if s.enabled]


def resolve_secret(reference: str | None) -> str | None:
    """Read a `env:NAME` reference. A literal is returned as-is but discouraged.

    Returning ``None`` for an unset variable rather than raising is deliberate:
    a supplier whose key is missing is a supplier klm reports as unconfigured,
    not a crash on startup for users who only enabled one of the two.
    """
    if not reference:
        return None
    if reference.startswith(ENV_PREFIX):
        return os.environ.get(reference[len(ENV_PREFIX) :].strip()) or None
    return reference


def default_aliases() -> dict[str, str]:
    """The built-in alias map, folded to lookup keys."""
    table: dict[str, str] = {}
    for canonical, spellings in fields.DEFAULT_ALIASES.items():
        for spelling in spellings:
            table[fields.alias_key(spelling)] = canonical
    return table


def default_suppliers() -> dict[str, SupplierConfig]:
    """The two suppliers klm ships knowing about (docs/01 — P1).

    TME defaults to API mode and is inert until credentials exist. LCSC
    defaults to **manual** mode, because LCSC's API is granted per company
    rather than per person and klm must be fully useful to someone who will
    never be granted it (docs/adr/0009).
    """
    return {
        "tme": SupplierConfig(
            name="tme",
            enabled=True,
            mode="api",
            api_key_ref="env:TME_API_KEY",
            api_secret_ref="env:TME_API_SECRET",
            currency="PLN",
            shipping_flat=15.0,
            free_shipping_above=250.0,
            # TME's API quotes net prices, so a consumer pays this on top. A
            # VAT-registered business should set it to 0 — and must set it to 0
            # on *both* suppliers, or the comparison tilts (docs/14 Q8).
            vat_rate=0.23,
        ),
        "lcsc": SupplierConfig(
            name="lcsc",
            enabled=True,
            mode="manual",
            api_key_ref="env:LCSC_API_KEY",
            api_secret_ref="env:LCSC_API_SECRET",
            currency="USD",
            shipping_flat=12.0,
            # Import VAT has had no de-minimis since July 2021: every parcel is
            # taxed at the destination rate. Leaving this at zero while TME
            # carried 23% made the imported supplier look cheaper than it is,
            # which is precisely the comparison this figure exists to inform.
            vat_rate=0.23,
            import_charges=True,
        ),
    }


def load_config(path: Path | None) -> Config:
    """Read `config.toml`, or return defaults when it is absent.

    A missing file is normal. A malformed one is not: klm raises rather than
    falling back, because silently ignoring a config means linting against
    rules the user believes they changed.
    """
    aliases = default_aliases()
    if path is None or not path.exists():
        return Config(fields=FieldConfig(aliases=aliases), suppliers=default_suppliers())

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

    suppliers = _suppliers(_table(data, "suppliers", path), path)

    sourcing = _table(data, "sourcing", path)
    stale_days = sourcing.get("stale_days", DEFAULT_STALE_DAYS)
    if not isinstance(stale_days, int) or stale_days < 1:
        raise ConfigError(f"{path}: [sourcing] stale_days must be a positive integer")

    return Config(
        fields=FieldConfig(aliases=aliases, custom=custom),
        lint=lint,
        suppliers=suppliers,
        stale_days=stale_days,
        significant_figures=significant,
        source=path,
    )


_SUPPLIER_MODES = ("api", "manual")


def _suppliers(section: dict[str, Any], path: Path) -> dict[str, SupplierConfig]:
    """Merge `[suppliers.*]` onto the built-in defaults, block by block.

    Merged rather than replaced for the same reason field aliases are: a user
    setting one key on TME should not silently reset its VAT rate to zero.
    """
    suppliers = default_suppliers()
    for name, raw in section.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: [suppliers.{name}] must be a table")
        base = suppliers.get(name, SupplierConfig(name=name))
        mode = str(raw.get("mode", base.mode))
        if mode not in _SUPPLIER_MODES:
            raise ConfigError(
                f"{path}: [suppliers.{name}] mode must be one of {', '.join(_SUPPLIER_MODES)}"
            )
        for secret_key in ("api_key", "api_secret"):
            value = raw.get(secret_key)
            if isinstance(value, str) and value and not value.startswith(ENV_PREFIX):
                raise ConfigError(
                    f"{path}: [suppliers.{name}] {secret_key} must be an 'env:NAME' reference, "
                    "not the secret itself"
                )
        suppliers[name] = SupplierConfig(
            name=name,
            enabled=bool(raw.get("enabled", base.enabled)),
            mode=mode,
            api_key_ref=str(raw.get("api_key", base.api_key_ref or "")) or None,
            api_secret_ref=str(raw.get("api_secret", base.api_secret_ref or "")) or None,
            currency=str(raw.get("currency", base.currency)),
            rate_per_second=float(raw.get("rate_per_second", base.rate_per_second)),
            burst=float(raw.get("burst", base.burst)),
            shipping_flat=float(raw.get("shipping_flat", base.shipping_flat)),
            free_shipping_above=_optional_float(raw, "free_shipping_above", base),
            vat_rate=float(raw.get("vat_rate", base.vat_rate)),
            import_charges=bool(raw.get("import_charges", base.import_charges)),
        )
    return suppliers


def _optional_float(raw: dict[str, Any], key: str, base: SupplierConfig) -> float | None:
    if key not in raw:
        return base.free_shipping_above
    value = raw[key]
    return None if value is None else float(value)


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
