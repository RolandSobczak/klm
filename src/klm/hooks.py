"""The pre-commit hook klm installs into a repository.

Linting on save is a courtesy; linting at commit is the layer that actually
keeps a shared repository clean, because it runs whether or not the person
committing remembered (docs/05 §6).

The hook is `language: system` and calls the installed `klm`, rather than
pinning a revision of this repository: a catalog and its linter should not be
able to drift apart by version.
"""

from __future__ import annotations

__all__ = ["HOOK_BLOCK", "HOOK_ID", "PRE_COMMIT_CONFIG", "PRE_COMMIT_TEMPLATE"]

PRE_COMMIT_CONFIG = ".pre-commit-config.yaml"
HOOK_ID = "klm-lint"

HOOK_BLOCK = """\
  - repo: local
    hooks:
      - id: klm-lint
        name: klm lint
        entry: klm lint --max-severity warning
        language: system
        pass_filenames: false
        files: \\.(kicad_sch|kicad_pcb|kicad_sym|kicad_mod|yaml)$
"""

PRE_COMMIT_TEMPLATE = f"""\
# Managed by `klm hook`. Edit freely; klm only checks that the hook is present.
repos:
{HOOK_BLOCK}"""
