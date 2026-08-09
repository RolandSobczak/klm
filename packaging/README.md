# Packaging

How klm becomes something you install by clicking Next.

| File | What it is |
|---|---|
| `entry_app.py` / `entry_cli.py` | The two frozen entry points. They call `klm.cli.main`, never a copy of it |
| `klm.spec` | PyInstaller: one analysis, two executables, one `dist/klm/` — plus a `.app` on macOS |
| `klm.iss` | Inno Setup: the Windows wizard, shortcuts, PATH, WebView2 check |
| `make_icons.py` | `static/logo.png` → `klm.ico` and `klm.icns` |
| `../.github/workflows/ci.yml` | The gate: ruff, mypy, pytest on three platforms; wheel and sdist |
| `../.github/workflows/release.yml` | Builds the downloadable klm for all three platforms |

## Getting a build

**Tag a release.** `git tag v0.1.0 && git push --tags` builds every platform and
attaches all of it to the GitHub release.

**Or build from any branch** without tagging: Actions → *Release* → *Run
workflow*. Everything lands in that run's artifacts, downloadable for 90 days.
This is the way to test a change to packaging, because the alternative is
tagging a version to find out whether the tag works.

**Or build locally**, for the platform you are on:

```bash
pip install -e ".[app]" pyinstaller pyinstaller-hooks-contrib pillow
python packaging/make_icons.py
pyinstaller packaging/klm.spec --noconfirm --clean
# Windows only, for the installer itself:
# & "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DKlmVersion=0.1.0 packaging\klm.iss
```

Only for the platform you are on. **PyInstaller does not cross-compile** — what
it produces is that interpreter's own binaries plus a bootloader, so a Windows
build needs a Windows Python. That is the whole reason the workflow exists: this
repository is developed on Linux, and without runners the releases would be
built by hand, occasionally, from whatever happened to be installed — which is
how a release comes to ship a different klm than the one that was tested.

## What each platform gets

| Platform | Artifact | Shape |
|---|---|---|
| Windows | `klm-setup-<v>.exe` | Inno Setup wizard; installs `klm-app.exe` and `klm.exe` |
| macOS | `klm-<v>-macos.zip` | `klm.app`, drag to Applications |
| Linux | `klm-<v>-linux-x86_64.tar.gz` | A directory with `klm-app` and `klm` in it |
| Any | `klm-<v>-py3-none-any.whl` | `pip install 'klm[app]'` |

**The CLI ships with every one of them.** On Windows it goes on `PATH` if the
box is ticked; on macOS it is `klm.app/Contents/MacOS/klm`; in the tarball it is
next to the app. This is not a convenience — the project's rule is that if the
GUI can do something the CLI cannot, that is a bug in the CLI, and shipping only
the window would make that unenforceable for anyone who installed the easy way.

The Windows install is **per-user by default**, so no UAC prompt: klm writes
only to the user's own catalog and projects, and asking for administrator to do
that would be asking for more than it needs. The wizard still offers
per-machine. Uninstalling removes the `PATH` entry it added.

## Four things that will bite

**PyInstaller and uvicorn.** uvicorn resolves its loop, protocol and lifespan
implementations *by name* at runtime, so static analysis finds none of them. A
build missing them starts fine and dies on the first request — the worst shape
of failure, because it passes every check that does not make an HTTP call. They
are listed explicitly in `klm.spec` rather than left to a hook, and the workflow
starts the frozen server and fetches `/api/health` and `/static/app.js` before
it packages anything.

**WebView2.** pywebview draws through Microsoft's WebView2 runtime. Windows 11
and updated Windows 10 have it; not every machine does. The installer checks and
offers the official bootstrapper, but never blocks on it — klm's rule is that
absence degrades rather than fails, and without WebView2 `klm app` already falls
back to `klm serve` and a browser URL.

**Nothing is code-signed.** On Windows the first person to run the installer
gets *"Windows protected your PC"* and has to choose *More info* → *Run anyway*.
On macOS it is worse: Gatekeeper refuses an unsigned, unnotarised `.app`
outright, and the user has to right-click → *Open*, or run
`xattr -dr com.apple.quarantine /Applications/klm.app`. Neither has a trick
around it.

- **Windows** needs an Authenticode certificate — roughly €200–400/year for OV,
  which still accrues SmartScreen reputation slowly, or more for EV, which is
  trusted immediately. Add the `.pfx` and its password as repository secrets and
  `signtool sign` the installer after the Inno step.
- **macOS** needs an Apple Developer account (99 USD/year) plus *notarisation* —
  `codesign --deep --options runtime`, then `xcrun notarytool submit`, then
  `xcrun stapler staple`. Signing without notarising does not help.

Both are single steps in `release.yml` once the certificates exist. Until then,
the release notes say so plainly rather than letting a user discover it.

**`klm doctor` exits 1 by design.** It is a check, and klm's convention is 0 ok,
1 the check failed, 2 klm errored. A runner has no KiCad, so the smoke test
tolerates 1 and fails only on 2 or worse. Treating any non-zero as a build
failure makes the release job red on every run, which is exactly how a red build
comes to be ignored.

**The Linux tarball is glibc-bound.** It links against the runner's glibc and
will not start on a much older distribution. There is no fix inside this
workflow — an AppImage or a `manylinux` build would be one, and neither is worth
it while `pip install 'klm[app]'` is the better route on Linux anyway.

`onedir`, not `onefile`, by the way. A one-file build unpacks itself to a
temporary directory on every launch: seconds of cold start, a `sys.executable`
that will not exist next time, and a shape that aggressive antivirus dislikes.
An installer is already copying a directory, so the one thing onefile buys is
the one thing we do not need.
