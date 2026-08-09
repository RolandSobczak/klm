# 14 — Open Questions

Things this documentation asserts or assumes that have **not been verified**, and must be before
anything is built on them. Each has an owner phase from [13 — Roadmap](13-roadmap.md).

Treat this as the risk register. Anything here that turns out worse than assumed changes the
design, not just the implementation.

---

## Q1 — TME API authentication mechanism — **RESOLVED** (2026-08-09)

**Blocked:** Phase 2 · **Answer:** signature-based, as this document suspected. Not OAuth.

The original design note was wrong. TME's scheme is request signing:

- An application **token** and **secret**, obtained by registering at `developers.tme.eu`. There is
  no authorization flow, no bearer token, and nothing to refresh.
- Each request carries an HMAC-SHA1 signature over an OAuth-1.0a-*style* base string —
  `POST&<percent-encoded endpoint URL>&<percent-encoded sorted query string>` — base64-encoded and
  sent as the `ApiSignature` parameter. The similarity to OAuth 1.0a's signing is presumably where
  the "OAuth" claim came from; the resemblance stops at the base string.
- Parameters are sorted by name and flattened into `Name[0]` / `Name[Key]` form *before* signing,
  so the flattening is part of the signature, not a transport detail.
- Tokens come in two kinds: anonymous (public data) and private (linked to a TME customer
  account). klm needs only the former for sourcing.
- A bad signature returns `E_INVALID_SIGNATURE` and nothing more diagnostic, which is why
  `klm.suppliers.tme.signature_base` is a public pure function with its own tests.

**Still unverified:** the published rate limits, and whether the current v2 API differs from the
documented scheme above. `developers.tme.eu` puts its reference behind a login, so confirming
either needs an account. klm's rate limiter defaults conservatively (2 req/s) for that reason, and
the auth layer is one function deep if v2 turns out to differ.

---

## Q2 — LCSC API legitimacy and stability — **RESOLVED** (2026-08-09)

**Blocked:** Phase 2 · **Answer:** an official API now exists, and klm's users cannot have it.

This was the register's highest-risk item, and the answer went the way the document warned it
might. The last line of the old text — *"if the answer is bad, manual mode becomes the primary path
and the effort goes elsewhere"* — is what happened.

What was found:

- **An official LCSC API exists**, roughly eight services covering search, pricing, stock and
  ordering.
- **Access is per company, not per person.** The application requires a company website, a business
  licence or equivalent, contact details, an estimated order quantity, and a cooperation mode.
  Applicants who don't qualify are pointed at authorised third-party procurement providers
  (Luminovo, CalcuQuote).
- **The terms are restrictive**: no sharing the API documentation with third parties, no
  aggregating the data into other public or commercial APIs, no providing retrieved material to
  third parties, no selling derived information, no sharing credentials outside the holder's
  company.

klm's user is a hobbyist ordering a few hundred parts a year. They will not be granted access, and
neither have klm's authors — who therefore cannot legitimately hold the documentation needed to
write a client on that user's behalf.

**Decision:** [ADR-0009](adr/0009-lcsc-manual-first.md). Manual entry is promoted from fallback to
primary; `mode = "api"` is a seam for a user who *has* been granted access to plug in their own
client; and klm ships no code against unofficial endpoints in any mode. The risk is closed rather
than deferred, because nothing in klm now depends on an endpoint that can disappear.

---

## Q3 — JLCPCB rotation correction data — **RESOLVED** (2026-08-09)

**Blocked:** Phase 5 · **Answer:** worse than assumed, and it moved the key rather than the data.

The old text guessed that the values "vary by footprint library, change over time, and disagree
between sources". Two of those three turned out to be wrong in an instructive way.

- **JLCPCB publishes nothing authoritative.** Their own KiCad guide gives no per-package values; it
  tells the reader to install a third-party plugin and tick a box.
- **The sources do not disagree, because there is one source.** `Bouni/kicad-jlcpcb-tools` says
  outright that it adopted its corrections from `matthewlai/JLCKicadTools`; KiBot cites the same
  project. That original is **GPL-3.0**, and klm is MIT.
- **The index is wrong, not just the values.** KiBot's notes state it plainly: *"you can have two
  components with the same footprint and different rotations in the same project."* The correct
  orientation is a property of how the part sits in its reel — chosen per manufacturer part number
  — and the land pattern only correlates with it. A footprint-keyed table cannot represent the
  disagreement, so it answers confidently and sometimes wrongly.
- Separately: KiCad mirrors bottom-side components and JLCPCB does not. That is a whole-layer
  transform belonging in the fab profile, not a per-package correction.

The route the community is moving toward — deriving rotation from EasyEDA's fully-qualified
footprint name — is closed to klm by [ADR-0009](adr/0009-lcsc-manual-first.md), independently.

**Decision:** [ADR-0011](adr/0011-rotation-corrections-are-learned-not-bundled.md). klm bundles no
rotation data, and records corrections **per part** first, per footprint pattern second — which it
can do and the community tools cannot, because it has a catalog. The fallback this entry named
("ships with an empty table and learns from run one") is what happened.

**The cost, stated:** the first board of any new package is unprotected, where a bundled table
would have been right most of the time. `klm fab` reports which references carry no confirmed
correction so the fab's DFM preview gets a careful look; there is no stronger mitigation.

---

## Q4 — Licensing of imported library assets — **still open**, and now load-bearing

**Blocks:** Phase 3 (the EasyEDA importer) · **No longer blocks Phase 4**

Redistribution status of EasyEDA/LCSC-derived symbols, footprints and 3D models is unclear. This
matters specifically because vendored projects are intended for GitHub.

**Checked, August 2026 — the finding is that there is no finding.** EasyEDA's terms grant a
personal/internal-business licence to use the *service* and prohibit reselling it; user-contributed
library content has no default licence, with an optional CC-BY-SA the uploader may choose; and
nothing published addresses redistribution of vendor-supplied component assets specifically. Note
also that `easyeda2kicad.py`, the reference implementation everyone points at, is **AGPL-3.0**,
which would matter to anything that vendored its code.

An absent answer is not a permissive one. Combined with
[ADR-0009](adr/0009-lcsc-manual-first.md) — klm ships no client against unofficial endpoints, and
the EasyEDA component API is exactly such an endpoint — this is why **Phase 3 shipped without the
EasyEDA importer**. Two independent reasons pointed the same way, so the importer was not written
rather than written and disabled.

**To verify, if it is ever worth revisiting:** an explicit EasyEDA/LCSC statement on derived
library assets; how comparable tools handle attribution; whether KiCad's library license exception
covers derived works of this kind.

**Consolation:** the sources klm does use — KiCad's own libraries and its own generators — have no
such question. KiCad's libraries are permissively licensed with an explicit design-use exception,
and klm records that on every asset it takes (`asset.license_note`), so `klm licenses --project .`
will have something true to report when it arrives.

**Why it stopped blocking Phase 4 (August 2026).** Vendoring copies catalog assets into a
repository intended for GitHub, which is exactly the case this question covers — but the importer
that would have produced assets with an unclear status was never written, so there are none to
vendor. Every asset klm can hold today comes from KiCad's libraries or its own generators. The
question stays open for whenever an unclear source is added, and `klm licenses --project .` is
where it will surface.

**klm will not attempt to give legal advice.** It reports origin and flags uncertainty.

---

## Q5 — KiCad version targeting and the IPC API

**Blocks:** Phase 0 (version detection), unscheduled (IPC)

Assumptions to check:
- Which KiCad version to target as the baseline. KiCad 9 is current; the config path is
  version-pinned (`~/.config/kicad/<version>/`), so klm detects rather than hardcodes it.
- Whether the S-expression schema for `.kicad_sym`, `.kicad_mod`, `.kicad_sch` and `.kicad_pcb`
  differs enough between 8 and 9 to matter. The lossless round-trip design should absorb this —
  that's largely why it exists — but it needs testing against both.
- `kicad-cli` command surface and flag stability across versions.
- The **IPC API** introduced in recent KiCad for external tools to communicate with a running
  instance. If usable, it enables "push this part into the open project" and live sync. Attractive,
  entirely optional, and explicitly unscheduled until verified.

**If wrong:** contained by design if the round-trip guarantee holds. Verify it early with a real
corpus of files from both versions.

---

## Q6 — Label printing hardware

**Blocks:** Phase 7 (only the direct-printing part)

The 14×14 mm label target implies a small thermal label printer. Niimbot and Brother QL devices
are common in this space but use different, largely undocumented protocols.

**To verify:** which printer is actually available, and whether its vendor software accepts a
generated image.

**Design position:** klm emits **images and PDFs**, not printer protocol. Vendor software does the
printing. Direct device support is only considered if a specific device is chosen and a usable
library exists.

**Also unverified:** whether Data Matrix at ~6×6 mm is reliably scannable by the phone or scanner
actually used. Test with a printed sample before committing — if it isn't, the fallback is a
human-readable short ID and no barcode, which is a smaller loss than it sounds.

---

## Q7 — S-expression parsing library choice

**Blocks:** Phase 0

Whether to use an existing KiCad-format Python library or write a generic S-expression
reader/writer.

**Current position:** write a generic one. [ADR-0002](adr/0002-lossless-sexpr-round-trip.md) sets
out the reasoning — typed parsers discard unknown nodes and couple to one format version, which
directly conflicts with the never-lose-user-data goal.

**To verify:** whether a maintained library exists that preserves unknown nodes and round-trips
losslessly. If one does, use it; the requirement is the guarantee, not the authorship.

---

## Q8 — VAT, customs and import charge modelling

**Blocks:** Phase 6

Landed-cost comparison between a domestic PLN supplier and an imported USD one requires modelling
import VAT and any applicable duty. Rates, thresholds and collection mechanisms change, and
getting them wrong produces confidently incorrect financial figures.

**Design position:** all rates and thresholds are **configuration**, never code. Every computed
total is labelled an estimate, with its assumptions listed. klm does not present tax figures as
authoritative, and the user is expected to set the values that apply to them.

**To verify:** current rates and thresholds at implementation time, and how they're actually
collected in practice for the order sizes involved.

---

## Q9 — Catalog scale and performance

**Blocks:** nothing yet; revisit if assumptions break

The design assumes: hundreds to low thousands of parts, tens of projects, single user, single
machine. At that scale SQLite is comfortable by orders of magnitude and no performance work is
warranted.

**Watch for:** a catalog beyond ~10,000 parts, 3D assets beyond ~1 GB, or a git repository slow
enough to be annoying. Any of those would justify revisiting the storage design — none is
expected.

---

## Q10 — Parametric search quality at TME and LCSC

**Blocks:** Phase 9 (agent quality)

The research agent's usefulness depends on how good the suppliers' parametric search actually is.
If parametric filtering is weak or inconsistently populated, the agent falls back to keyword
search plus datasheet reading — which works but is slower and more expensive per session.

**To verify:** field coverage and consistency for the categories that matter (regulators, MCUs,
passives) in each supplier's API.

**If wrong:** an interesting mitigation the architecture already permits — use DigiKey's or
Mouser's official API purely as a *parametric search index* (they're excellent at it) and then
resolve the resulting MPNs to TME/LCSC offers for actual purchase. Free, legitimate, and it
sidesteps the weakness entirely.

---

## Q11 — KiCad in CI: container, headless rendering, determinism — **RESOLVED** (2026-08-09)

**Blocked:** Phase 6 · **Answer:** three of four settled, and one of them was a workflow-breaker.

**The container.** KiCad publishes official images at `kicad/kicad` (Docker Hub and GHCR), built
from `kicad/packaging/kicad-cli-docker` and intended precisely for `kicad-cli` in CI. Tags come in
three shapes: `9.0` tracks the latest patch, `9.0.9` pins one exactly, and `-full` variants add the
standard symbol, footprint and 3D libraries. Current stable at the time of checking: **10.0.5**
(Debian trixie) and **9.0.9** (bookworm). Scaffold pins an exact patch tag.

**The image runs as a non-root user.** Both `Dockerfile.9.0-stable` and `Dockerfile.10.0-stable`
end with `USER $USER_NAME`. In a GitHub Actions *container job* the workspace is created by the
runner as root, so `actions/checkout` fails on permissions before klm is ever invoked. The
generated workflow therefore sets `options: --user root` on the container. This was not in the
design, would have broken the first run of every scaffolded repository, and is the single most
valuable thing this check turned up.

**`-full` is the right image, and it settles a question ADR-0010 deferred.** Clean-room
verification asks "does this resolve on a machine that has nothing?" — but KiCad's own standard
libraries ship *with KiCad*, so any machine that can open the project has them. The honest
definition is therefore: no klm catalog, no klm global libraries, no user configuration, but a
stock KiCad install. That is exactly what the `-full` image provides, and it means
`klm verify --clean-room` can answer the `power:GND` versus `Passive:0R_0603` question by *trying
to resolve them*, with no list to maintain and no dependence on the author's machine — which is
what [ADR-0010](adr/0010-vendoring-leaves-unmanaged-libraries-linked.md) said Phase 6 would supply.

**Gerber timestamps: KiCad offers no way out, so klm rewrites them.** `SOURCE_DATE_EPOCH` — the
reproducible-builds standard — is **not** honoured. KiCad's `GbrMakeCreationDateAttributeString`
reads the wall clock unconditionally (`wxDateTime date( wxDateTime::GetTimeNow() )`); there is no
environment override of any kind. So byte-reproducible output requires post-processing the emitted
files, which is what `klm fab --normalize-timestamps` does.

The design note said it "zeroes" the timestamps, and that is what raised the parser worry. klm
substitutes a **fixed valid** ISO-8601 timestamp instead. A well-formed `%TF.CreationDate…*%` with
an unchanging value carries no parser risk, where a blanked field plausibly would — so the risk the
question was really about is closed by construction rather than by testing against an upload.

**Still unverified, and honestly so:** whether any `kicad-cli` export path needs an X server on
9.0/10.0. `pcb render` remains the suspect. The scaffolded workflow wraps render steps in
`xvfb-run` defensively, which costs nothing if unnecessary. Also unverified: the exact `kicad-cli`
flag surface, because KiCad is not installed on the development machine — the first real CI run is
what confirms it, and that is a known and accepted gap.

---

## Q12 — Interactive BOM and other third-party CI tools

**Blocks:** Phase 6 (optional artifacts only)

The artifacts workflow lists an interactive BOM as an optional output, produced by a community
tool rather than by klm.

**To verify:** current maintenance status, licensing, KiCad-version compatibility, and whether it
is packaged in a form that's safe to pin in CI.

**Position:** anything third-party in the artifact pipeline is optional and individually
switchable in `klm.toml`. A dead upstream should degrade one artifact, never the workflow.
