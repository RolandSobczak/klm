"""Register klm's libraries with the user's KiCad installation.

This is the first code that writes *outside* klm's own directory, into files
shared with every other library the user has installed. Three rules follow
(docs/03 §9):

* **Merge, never replace.** Only rows klm owns are touched; everything else is
  re-serialised byte-identically by the lossless writer.
* **Back up first.** Every file is copied beside itself before being changed.
* **Say what will happen.** ``plan`` computes the change set without touching
  anything, so ``--dry-run`` and ``--check`` are the same code path as the real one.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from klm.kicad.libtable import (
    LibEntry,
    TableKind,
    load_table,
    read_entries,
    upsert_entry,
    write_table,
)
from klm.store.paths import Paths

__all__ = ["LIBRARY_NAME", "RegistrationPlan", "apply_plan", "plan_registration"]

LIBRARY_NAME = "KLM"
LIBS_VAR = "KLM_LIBS"
MODELS_VAR = "KLM_3DMODELS"
_DESCR = "klm managed library"


@dataclass
class Change:
    target: Path
    description: str


@dataclass
class RegistrationPlan:
    kicad_config: Path
    changes: list[Change] = field(default_factory=list)
    already_correct: list[str] = field(default_factory=list)

    @property
    def needed(self) -> bool:
        return bool(self.changes)


def plan_registration(paths: Paths, kicad_config: Path) -> RegistrationPlan:
    """Work out what registering would change, without changing anything."""
    plan = RegistrationPlan(kicad_config=kicad_config)

    for filename, kind, entry in (
        (
            "sym-lib-table",
            TableKind.SYMBOL,
            LibEntry(LIBRARY_NAME, f"${{{LIBS_VAR}}}/KLM.kicad_sym", descr=_DESCR),
        ),
        (
            "fp-lib-table",
            TableKind.FOOTPRINT,
            LibEntry(LIBRARY_NAME, f"${{{LIBS_VAR}}}/KLM.pretty", descr=_DESCR),
        ),
    ):
        target = kicad_config / filename
        existing = {e.name: e for e in read_entries(load_table(target, kind))}
        current = existing.get(entry.name)
        if current is None:
            plan.changes.append(Change(target, f"add library '{entry.name}' → {entry.uri}"))
        elif current.uri != entry.uri or current.type != entry.type:
            plan.changes.append(
                Change(target, f"update '{entry.name}': {current.uri} → {entry.uri}")
            )
        else:
            plan.already_correct.append(f"{filename}: {entry.name}")

    common = kicad_config / "kicad_common.json"
    wanted = _wanted_vars(paths)
    current_vars = _read_env_vars(common)
    for name, value in wanted.items():
        if current_vars.get(name) != value:
            action = "set" if name not in current_vars else "update"
            plan.changes.append(Change(common, f"{action} {name} = {value}"))
        else:
            plan.already_correct.append(f"kicad_common.json: {name}")

    return plan


def apply_plan(paths: Paths, kicad_config: Path) -> RegistrationPlan:
    """Register klm with KiCad, backing up every file first."""
    plan = plan_registration(paths, kicad_config)
    if not plan.needed:
        return plan

    kicad_config.mkdir(parents=True, exist_ok=True)
    for target in sorted({change.target for change in plan.changes}):
        _backup(target)

    for filename, kind, entry in (
        (
            "sym-lib-table",
            TableKind.SYMBOL,
            LibEntry(LIBRARY_NAME, f"${{{LIBS_VAR}}}/KLM.kicad_sym", descr=_DESCR),
        ),
        (
            "fp-lib-table",
            TableKind.FOOTPRINT,
            LibEntry(LIBRARY_NAME, f"${{{LIBS_VAR}}}/KLM.pretty", descr=_DESCR),
        ),
    ):
        target = kicad_config / filename
        doc = load_table(target, kind)
        if upsert_entry(doc, entry):
            write_table(doc, target)

    _write_env_vars(kicad_config / "kicad_common.json", _wanted_vars(paths))
    return plan


def _wanted_vars(paths: Paths) -> dict[str, str]:
    return {
        LIBS_VAR: str(paths.generated),
        MODELS_VAR: str(paths.generated_models3d),
    }


def _read_env_vars(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    variables = data.get("environment", {}).get("vars")
    return dict(variables) if isinstance(variables, dict) else {}


def _write_env_vars(path: Path, wanted: dict[str, str]) -> None:
    """Merge variables into ``kicad_common.json``, preserving every other key.

    KiCad stores far more than environment variables in this file. Reading,
    editing two keys and writing back is the only safe shape.
    """
    data: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            # A corrupt config is the user's to fix; klm must not overwrite it.
            raise

    environment = data.get("environment")
    if not isinstance(environment, dict):
        environment = {}
        data["environment"] = environment
    variables = environment.get("vars")
    if not isinstance(variables, dict):
        variables = {}
        environment["vars"] = variables
    variables.update(wanted)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".klm-tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _backup(target: Path) -> Path | None:
    if not target.exists():
        return None
    backup = target.with_name(target.name + ".klm-bak")
    shutil.copyfile(target, backup)
    return backup
