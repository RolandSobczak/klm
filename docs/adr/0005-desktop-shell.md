# ADR-0005 — Python core + local HTTP API + Tauri shell

**Status:** accepted · **Phase:** 8 · **Related:** [12](../12-cli-and-desktop-app.md)

## Context

The stated goal is a desktop application. The core must be Python — KiCad's scripting, FreeCAD,
`easyeda2kicad` and the surrounding ecosystem all are, and any other language means shelling out
to Python anyway. The question is what the window is made of.

## Options considered

**A. PySide6 / Qt.** Single process, single language, native look, matches KiCad's own toolkit.
But desktop Qt UI development is slow going for data-dense screens with previews and diffs, the
Qt licensing story needs care for redistribution, and packaging PySide6 apps is its own project.

**B. Electron + Python sidecar.** Best-in-class UI tooling. Bundles Chromium: ~150 MB for a tool
whose job is managing library files.

**C. Tauri + Python sidecar.** Web UI, but rendered by the OS webview — a fraction of the size.
Two processes and an IPC boundary to manage.

**D. Local web app, browser only.** Simplest of all: FastAPI serving a UI, opened in the user's
browser. Not a desktop app; awkward lifecycle (who starts the server?); poor filesystem
integration for a tool that constantly touches local paths.

## Decision

**Option C**, with a hard structural constraint:

> The shell is a **client** of the local HTTP API. No business logic lives in it. Replacing Tauri
> with PySide6, Electron, or nothing at all must cost only UI work.

```
Tauri window ──HTTP/SSE──▶ FastAPI (localhost) ──▶ klm services ──▶ store / kicad / suppliers
                                    ▲
                                    └── klm CLI uses the same services directly
```

## Rationale

- The UI work is genuinely web-shaped: data tables, diff views, SVG previews of symbols and
  footprints, streaming agent output. Web tooling is far ahead for all of it.
- The API has to exist regardless — the desktop app needs it, and it doubles as a scripting
  surface for anyone who wants one.
- Tauri's binary is small enough not to feel absurd next to KiCad.
- Being wrong is cheap. Because the shell holds no logic, swapping it is a UI rewrite, not a
  system rewrite. That is the actual reason to choose it: it is the option whose failure costs
  least.

## Consequences

**Good**
- Fast iteration on data-dense screens.
- The API is a genuine product surface, not scaffolding.
- CLI and GUI cannot diverge — they call the same functions.
- Small distributable.

**Bad**
- Two processes with a lifecycle to manage: the shell must start, supervise and cleanly stop the
  Python sidecar, and surface a clear error if it dies.
- Rust in the toolchain for the shell, even if almost no Rust gets written.
- Webview differences across platforms need testing.
- A local HTTP server is an attack surface. Mitigated: bind `127.0.0.1` only, require a token
  generated at startup and passed to the shell, and never expose an unauthenticated endpoint.

**Neutral**
- Packaging bundles a Python runtime. Standard, solved, still work.
- macOS is not an initial target.

## Escape hatch

If the two-process model proves annoying in practice, PySide6 against the same service layer is
a contained fallback, because the service layer is where everything actually lives. The decision
being reversible at moderate cost is what makes it safe to make now.
