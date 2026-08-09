# ADR-0012 — A pywebview shell, superseding the Tauri one

**Status:** accepted · **Phase:** 8 · **Supersedes:** [ADR-0005](0005-desktop-shell.md) ·
**Related:** [03](../03-architecture.md), [12](../12-cli-and-desktop-app.md)

## Context

[ADR-0005](0005-desktop-shell.md) chose Tauri for the desktop shell: a small binary, a native
webview, no bundled Chromium. That reasoning still holds on its own terms. What has changed is that
the rest of klm arrived, and the shape of the problem it leaves for the shell is now visible.

Three facts now decide this.

**The shell has no logic in it, by construction.** ADR-0005 said so and the code kept the promise:
every screen is a thin view over `klm.services.*`, and the API layer added in this phase is a
mechanical translation of functions that already exist and are already tested. Whatever renders the
window is therefore close to interchangeable — which makes the toolchain cost, not the framework's
merits, the deciding factor.

**Tauri means three build pipelines.** Rust plus npm, per platform, producing per-platform bundles,
each needing signing to install without a warning. klm is one person's project with no CI budget
for a Rust matrix and no code-signing certificates. The requirement is "runs on Windows, macOS and
Linux", and Tauri meets it by tripling the work.

**klm is already installed as Python on all three.** A user who has klm has a Python environment.
A shell that ships in the same wheel is available the moment `pip install klm[app]` finishes, on
every platform, with no second toolchain and nothing to sign.

## Options considered

**A. Tauri, as decided.** Genuinely the best result: a 5 MB signed native app. Rejected on cost —
Rust toolchain, npm build, three CI targets and certificates, to render views that contain no
logic. If klm ever has a maintainer with an interest in packaging, this becomes attractive again
and the API makes it a rewrite of the view layer only.

**B. Browser UI from `klm serve`.** The cheapest option, and it works everywhere. Rejected as the
*only* option: "open a terminal, run a command, then find the tab" is not a desktop app, and the
request was for one. It is kept as a mode, because it is genuinely the right answer over SSH and in
a container.

**C. pywebview.** A native window wrapping the platform's own webview — WebView2 on Windows,
WebKit on macOS, WebKitGTK on Linux. Pure Python, one dependency, ships in the same wheel, no
bundled browser engine. The window is native; the content is the same local UI option B serves.

## Decision

**Option C, with option B kept as a flag.** `klm app` opens a native window; `klm serve` prints a
URL and stays in the terminal. Both run the identical FastAPI application on localhost, and the UI
is one directory of static files with no build step — no bundler, no node_modules, no CDN.

The API keeps ADR-0005's constraint verbatim: **the shell contains no logic**, and every endpoint
is a translation of an existing service function. The rule that enforces it is unchanged — *if the
GUI can do something the CLI cannot, that is a bug in the CLI.*

## Consequences

**Good**
- One codebase, three platforms, installed by `pip install klm[app]`. Nothing to sign, no Rust, no
  npm, no per-platform CI.
- The UI is plain files served over HTTP, so it is testable with `TestClient` in the same suite as
  everything else — which a Rust bundle would not have been here.
- `klm serve` falls out for free, and covers the headless and remote cases Tauri could not.

**Bad**
- **A heavier install than Tauri's.** klm plus FastAPI, uvicorn and pywebview against a single 5 MB
  binary. For a user who already has klm this is a few megabytes of pure-Python wheels, but it is
  not the tidy artifact ADR-0005 wanted.
- **The Linux webview is the weak platform.** WebKitGTK must be present (`gir1.2-webkit2-4.1` or
  equivalent); it usually is on a desktop install and is not in a minimal container. `klm app`
  detects its absence and falls back to `klm serve` with an explanation rather than a traceback.
- No native menus, no OS-level file dialogs beyond what the webview offers, no auto-update.
- A cold start pays Python's import cost, which a compiled binary does not.

**Neutral**
- The decision is reversible cheaply and deliberately so. The API is the product of this phase; a
  Tauri front end would consume the same endpoints, and nothing in `klm.services` or `klm.api`
  would change.
