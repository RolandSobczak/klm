"""Mapping between :class:`~klm.model.Part` and its ``part.yaml`` representation.

The field order here *is* the file format. It is fixed deliberately: identity
first, then description, then the machine-generated hashes, then parameters —
so a part reads top-to-bottom the way a person would describe it, and so that
two exports of the same content produce identical bytes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypeVar

from klm.model import Confidence, Lifecycle, Parameter, Part, PartStatus, SourceKind
from klm.serial.yaml import YamlError, dumps, loads

__all__ = ["FORMAT_VERSION", "from_yaml", "part_to_mapping", "to_yaml"]

#: Bumped when the on-disk shape changes, so an older export stays importable.
FORMAT_VERSION = 1

E = TypeVar("E", bound=StrEnum)


def part_to_mapping(part: Part) -> dict[str, Any]:
    """Build the ordered mapping written to ``part.yaml``.

    Optional fields are omitted when unset rather than written as ``null``: an
    absent line is quieter in a diff than a line that says nothing.
    """
    data: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "klm_id": part.klm_id,
        "mpn": part.mpn,
        "manufacturer": part.manufacturer,
    }
    if part.description:
        data["description"] = part.description
    for key, value in (
        ("category", part.category),
        ("package", part.package),
    ):
        if value:
            data[key] = value

    data["lifecycle"] = str(part.lifecycle)
    data["status"] = str(part.status)

    for key, value in (
        ("datasheet_url", part.datasheet_url),
        ("datasheet_sha", part.datasheet_sha),
        ("notes", part.notes),
    ):
        if value:
            data[key] = value

    assets = {
        key: value
        for key, value in (
            ("symbol", part.symbol_hash),
            ("footprint", part.footprint_hash),
            ("model3d", part.model3d_hash),
        )
        if value
    }
    if assets:
        data["assets"] = assets

    if part.parameters:
        data["parameters"] = [_parameter_to_mapping(p) for p in part.sorted_parameters()]

    for key, value in (("created_at", part.created_at), ("updated_at", part.updated_at)):
        if value:
            data[key] = value

    return data


def _parameter_to_mapping(parameter: Parameter) -> dict[str, Any]:
    data: dict[str, Any] = {"name": parameter.name}
    if parameter.value_num is not None:
        data["value"] = parameter.value_num
    elif parameter.value_text is not None:
        data["text"] = parameter.value_text
    if parameter.unit:
        data["unit"] = parameter.unit
    if parameter.tolerance is not None:
        data["tolerance"] = parameter.tolerance
    data["source"] = str(parameter.source)
    if parameter.source_ref:
        data["source_ref"] = parameter.source_ref
    data["confidence"] = str(parameter.confidence)
    return data


def to_yaml(part: Part) -> str:
    return dumps(part_to_mapping(part))


def from_yaml(text: str) -> Part:
    """Parse a ``part.yaml`` document.

    A missing required field or an unknown enum value raises rather than being
    defaulted — silently repairing a malformed catalog file would hide the very
    corruption the export exists to make visible.
    """
    data = loads(text)

    version = data.get("format_version", FORMAT_VERSION)
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise YamlError(
            f"part.yaml format version {version} is newer than this klm understands "
            f"(supports up to {FORMAT_VERSION})"
        )

    assets = data.get("assets") or {}
    if not isinstance(assets, dict):
        raise YamlError("'assets' must be a mapping")

    return Part(
        klm_id=_required_str(data, "klm_id"),
        mpn=_required_str(data, "mpn"),
        manufacturer=_required_str(data, "manufacturer"),
        description=_optional_str(data, "description") or "",
        category=_optional_str(data, "category"),
        package=_optional_str(data, "package"),
        lifecycle=_enum(Lifecycle, data.get("lifecycle"), "lifecycle", Lifecycle.UNKNOWN),
        status=_enum(PartStatus, data.get("status"), "status", PartStatus.DRAFT),
        datasheet_url=_optional_str(data, "datasheet_url"),
        datasheet_sha=_optional_str(data, "datasheet_sha"),
        notes=_optional_str(data, "notes"),
        symbol_hash=_optional_str(assets, "symbol"),
        footprint_hash=_optional_str(assets, "footprint"),
        model3d_hash=_optional_str(assets, "model3d"),
        parameters=[_parameter_from_mapping(p) for p in data.get("parameters") or []],
        created_at=_optional_str(data, "created_at"),
        updated_at=_optional_str(data, "updated_at"),
    )


def _parameter_from_mapping(raw: object) -> Parameter:
    if not isinstance(raw, dict):
        raise YamlError("each parameter must be a mapping")
    value = raw.get("value")
    if value is not None and not isinstance(value, (int, float)):
        raise YamlError(f"parameter {raw.get('name')!r}: 'value' must be numeric")
    tolerance = raw.get("tolerance")
    if tolerance is not None and not isinstance(tolerance, (int, float)):
        raise YamlError(f"parameter {raw.get('name')!r}: 'tolerance' must be numeric")

    return Parameter(
        name=_required_str(raw, "name"),
        source=_enum(SourceKind, raw.get("source"), "source", None),
        value_num=float(value) if value is not None else None,
        value_text=_optional_str(raw, "text"),
        unit=_optional_str(raw, "unit"),
        tolerance=float(tolerance) if tolerance is not None else None,
        source_ref=_optional_str(raw, "source_ref"),
        confidence=_enum(Confidence, raw.get("confidence"), "confidence", Confidence.MEDIUM),
    )


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise YamlError(f"missing required field {key!r}")
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    return value


def _enum(enum_type: type[E], value: object, field_name: str, default: E | None) -> E:
    if value is None:
        if default is None:
            raise YamlError(f"missing required field {field_name!r}")
        return default
    valid = ", ".join(str(m) for m in enum_type)
    if not isinstance(value, str):
        raise YamlError(f"invalid {field_name} {value!r} (expected one of: {valid})")
    try:
        return enum_type(value)
    except ValueError:
        raise YamlError(f"invalid {field_name} {value!r} (expected one of: {valid})") from None
