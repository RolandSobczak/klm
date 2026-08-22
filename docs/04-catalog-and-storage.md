# 04 — Catalog and Storage

## 1. Why SQLite plus a flat-file export

Two requirements pull opposite ways: the catalog must be **queryable** (find every 0402 100nF
X7R ≥16 V with stock at TME) and **versionable** (a git history of the library, diffable, restorable).

- Flat files as truth: great diffs, unusable queries at 900 parts × 20 parameters.
- SQLite as truth: great queries, a binary blob in git with no meaningful history.

klm does both: **SQLite is the operational store; a deterministic YAML export is the git-versioned
mirror.** `klm export` writes it, `klm import` rebuilds the database from it. The database can
always be discarded and reconstructed. See [ADR-0001](adr/0001-sqlite-as-source-of-truth.md).

KiCad's own *database library* feature (`.kicad_dbl` over ODBC) would let KiCad read the SQLite
file directly, skipping generation entirely. It's rejected as the default because it requires
ODBC configuration on every machine and does nothing for the sharing problem — but it's a
supported optional mode for a power user working alone. See [ADR-0004](adr/0004-generated-flat-libraries.md).

## 2. Schema

Slightly simplified; indexes and constraints noted where they matter.

```sql
CREATE TABLE part (
  klm_id          TEXT PRIMARY KEY,          -- ULID, immutable
  mpn             TEXT NOT NULL,
  manufacturer_id INTEGER NOT NULL REFERENCES manufacturer(id),
  description     TEXT NOT NULL,
  category_id     INTEGER REFERENCES category(id),
  package         TEXT,
  lifecycle       TEXT NOT NULL DEFAULT 'unknown',
  status          TEXT NOT NULL DEFAULT 'draft',   -- draft|approved|deprecated
  datasheet_url   TEXT,
  datasheet_sha   TEXT,                            -- local cached PDF
  symbol_hash     TEXT REFERENCES asset(content_hash),
  footprint_hash  TEXT REFERENCES asset(content_hash),
  model3d_hash    TEXT REFERENCES asset(content_hash),
  notes           TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX part_mpn_mfr ON part(manufacturer_id, mpn) WHERE status != 'deprecated';

-- Merged duplicates keep resolving, so old schematics never break.
CREATE TABLE part_alias (
  alias_klm_id  TEXT PRIMARY KEY,
  klm_id        TEXT NOT NULL REFERENCES part(klm_id),
  merged_at     TEXT NOT NULL
);

CREATE TABLE parameter (
  klm_id      TEXT NOT NULL REFERENCES part(klm_id),
  name        TEXT NOT NULL,
  value_num   REAL,          -- SI base units
  value_text  TEXT,          -- for non-numeric
  unit        TEXT,
  tolerance   REAL,
  source_kind TEXT NOT NULL, -- user|datasheet|supplier|inferred
  source_ref  TEXT,          -- URL, page number, quote
  confidence  TEXT NOT NULL,
  PRIMARY KEY (klm_id, name, source_kind)   -- conflicting sources coexist
);

CREATE TABLE asset (
  content_hash TEXT PRIMARY KEY,   -- sha256 of canonical bytes
  kind         TEXT NOT NULL,      -- symbol|footprint|model3d
  filename     TEXT NOT NULL,
  source       TEXT NOT NULL,      -- generated|imported:easyeda|hand-drawn|...
  license_note TEXT,
  qa_status    TEXT NOT NULL,      -- pass|warn|fail|unchecked
  qa_report    TEXT,               -- JSON
  created_at   TEXT NOT NULL
);

CREATE TABLE offer (
  supplier      TEXT NOT NULL,
  supplier_pn   TEXT NOT NULL,
  klm_id        TEXT NOT NULL REFERENCES part(klm_id),
  packaging     TEXT,
  moq           INTEGER,
  multiple      INTEGER,
  stock         INTEGER,
  currency      TEXT,
  price_breaks  TEXT NOT NULL,     -- JSON [[qty, unit_price], ...]
  lead_time_days INTEGER,
  url           TEXT,
  fetched_at    TEXT NOT NULL,
  PRIMARY KEY (supplier, supplier_pn)
);
CREATE INDEX offer_part ON offer(klm_id);

CREATE TABLE project (
  path        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  mode        TEXT NOT NULL,       -- linked|vendored
  last_seen   TEXT
);

CREATE TABLE stock_item (
  klm_id       TEXT NOT NULL REFERENCES part(klm_id),
  location     TEXT NOT NULL,
  quantity     INTEGER NOT NULL,
  last_counted TEXT,
  PRIMARY KEY (klm_id, location)
);

CREATE TABLE purchase_order (
  id         TEXT PRIMARY KEY,
  supplier   TEXT NOT NULL,
  state      TEXT NOT NULL,        -- draft|placed|partially_received|received|cancelled
  currency   TEXT,
  placed_at  TEXT,
  received_at TEXT,
  notes      TEXT
);

CREATE TABLE purchase_line (
  order_id    TEXT NOT NULL REFERENCES purchase_order(id),
  klm_id      TEXT NOT NULL REFERENCES part(klm_id),
  supplier_pn TEXT NOT NULL,
  qty_ordered INTEGER NOT NULL,
  qty_received INTEGER NOT NULL DEFAULT 0,
  unit_price  REAL,
  PRIMARY KEY (order_id, supplier_pn)
);

-- Learned from real fabrication runs; see doc 09.
CREATE TABLE rotation_correction (
  pattern     TEXT PRIMARY KEY,    -- regex against footprint name
  rotation    REAL NOT NULL DEFAULT 0,
  offset_x    REAL NOT NULL DEFAULT 0,
  offset_y    REAL NOT NULL DEFAULT 0,
  source      TEXT NOT NULL,       -- bundled|user|learned
  confirmed_at TEXT
);

CREATE TABLE event_log (
  id        INTEGER PRIMARY KEY,
  at        TEXT NOT NULL,
  actor     TEXT NOT NULL,         -- user|agent|import
  action    TEXT NOT NULL,
  subject   TEXT,
  detail    TEXT                   -- JSON
);
```

`event_log` is append-only and cheap. It answers "why is this part like this?", which matters a
lot once an AI agent is contributing.

## 3. The YAML export

One directory per part, keyed by `klm_id`:

```yaml
# catalog/01JB4K7QW8ZR3XN5M2VYT9DCFA/part.yaml
klm_id: 01JB4K7QW8ZR3XN5M2VYT9DCFA
mpn: STM32F103C8T6
manufacturer: STMicroelectronics
description: ARM Cortex-M3 MCU, 64 kB flash, 20 kB RAM, LQFP-48
category: IC/Microcontroller/ARM
package: LQFP-48
lifecycle: active
status: approved
datasheet_url: https://www.st.com/resource/en/datasheet/stm32f103c8.pdf
assets:
  symbol: sha256:3f9a…
  footprint: sha256:71c4…
  model3d: sha256:aa02…
parameters:
  - {name: vdd_min, value: 2.0, unit: V, source: {kind: datasheet, page: 12}}
  - {name: vdd_max, value: 3.6, unit: V, source: {kind: datasheet, page: 12}}
  - {name: flash, value: 65536, unit: B, source: {kind: datasheet, page: 1}}
offers_snapshot:               # informational only; refreshed from the API
  - {supplier: tme, supplier_pn: STM32F103C8T6, fetched_at: 2026-08-01}
  - {supplier: lcsc, supplier_pn: C8734, fetched_at: 2026-08-01}
```

Export determinism rules — these are what make git history usable:

- Keys in a fixed order, never alphabetical-by-accident.
- Lists sorted by a stable key (parameters by name, offers by supplier).
- Floats formatted with fixed precision; no `1.0000000000000002`.
- No timestamps that change on every export. `updated_at` changes only when content changes.
- UTF-8, LF endings, trailing newline.

A property test asserts `export(import(export(db))) == export(db)` byte-for-byte.

### Sharing a catalog between machines

The mirror is what makes a catalog a git repository, and one rule governs the whole arrangement:

**`catalog/` and `assets/` travel together; nothing else in `$KLM_HOME` travels at all.**

`part.yaml` records an asset's content *hash*, not its bytes. A repository carrying `catalog/`
alone imports without a single error and produces parts with no symbol — the worst shape a
failure can take, because it looks like success. `klm doctor` checks for exactly this and fails:
a referenced asset that is not on this disk is reported, never passed over.

`klm init` writes a `.gitignore` for the rest, because the natural `git add .` sweeps up a binary
database nothing can merge, a `generated/` directory that is rebuilt from the catalog anyway, a
cache of datasheet PDFs, and a `config.toml` naming the environment variables that hold supplier
credentials. An existing `.gitignore` is left alone — it is the user's file.

```bash
klm export && git add catalog assets && git commit && git push      # this machine
git clone <repo> ~/.local/share/klm && klm import && klm generate   # the next one
```

What the mirror does **not** carry is as important: offers, orders, stock, approved
substitutions and the event log live in the database only. Sharing a catalog shares the library,
not the purchase history — the second is a record of one bench.

## 4. Asset storage and deduplication

Assets are content-addressed: the filename *is* the hash of the canonical bytes.

- Adding a part whose footprint matches an existing one costs zero bytes.
- A 3D model shared by every SOT-23 part is stored once. In a catalog of ~900 parts this is the
  difference between a ~2 GB repository and a ~150 MB one, because STEP files dominate.
- Mutating an asset is impossible by construction. "Fixing" a footprint produces a new hash;
  migrating parts to it is an explicit, listed, reviewable operation.

Canonicalization before hashing matters: two byte-different files that mean the same thing should
share a hash. For footprints and symbols, canonicalize via the S-expression writer (stable
formatting, sorted attributes, fixed float precision) before hashing. For STEP, hash the file as-is
— STEP contains generation timestamps, so klm strips the header lines that vary before hashing.

### Importing an existing library

`klm import --from-kicad` is the on-ramp, and it takes a *part*, not a symbol: given
`--library-dir`, it also stores the footprint the symbol's `Footprint` field names and the 3D
model that footprint references. A catalog of symbols alone is one whose parts stop working the
moment they leave the machine they were imported on.

Three rules, all of them the same rule:

- **A footprint that cannot be found is reported, not invented.** The part is still imported —
  it is worth having — but the missing land pattern is named, per symbol.
- **The model reference is resolved by basename**, against `--model-dir` (defaulting to
  `<library-dir>/../3dmodels`). The path a footprint records is usually absolute and usually
  wrong, having been written on whichever machine drew it. A reference to a `.wrl` is followed to
  the STEP beside it, since a mesh does not belong in a STEP-shaped slot.
- **A footprint from KiCad's own libraries is deliberately left unresolved** when KiCad is not
  installed. Those ship with KiCad on every machine, so copying them into the catalog buys
  nothing; the symbol keeps naming them and KiCad finds them.

### 3D models and git

STEP files are ASCII but large (0.5–5 MB). Guidance rather than enforcement:

- Deduplication is the first and biggest win — do that always.
- For the global catalog repository, `git-lfs` on `assets/models3d/` is recommended once the
  directory exceeds ~200 MB.
- For **vendored project** repositories, 3D models are optional. `klm vendor --no-3d` produces a
  repo that renders schematics and PCBs correctly but omits the mechanical models; this is
  usually the right choice for a collaboration repo, and it's the default.

## 5. Generation

`klm generate` rebuilds `generated/` from the catalog:

1. Query all `status = 'approved'` parts.
2. Assemble `KLM.kicad_sym` from symbol assets, injecting fields per the canonical schema
   ([05](05-field-schema-and-linting.md)) — including `KLM_ID`.
3. Copy footprint assets into `KLM.pretty/`, rewriting the `(model …)` path to
   `${KLM_3DMODELS}/<name>.step`.
4. Copy the referenced 3D models into `packages3d/`.
5. Write a manifest (`generated/manifest.json`) mapping each generated file to the catalog
   revision and asset hashes that produced it.

Generation is idempotent and byte-stable. Running it twice with no catalog change produces
identical files, which lets the desktop app run it after every edit without churn.

## 6. Registration with KiCad

klm adds three things to KiCad's user configuration, all merge-safe:

| File | What klm adds |
|---|---|
| `<kicad-config>/sym-lib-table` | One row: `KLM` → `${KLM_LIBS}/KLM.kicad_sym` |
| `<kicad-config>/fp-lib-table` | One row: `KLM` → `${KLM_LIBS}/KLM.pretty` |
| `<kicad-config>/kicad_common.json` | `environment.vars.KLM_LIBS`, `environment.vars.KLM_3DMODELS` |

Rules:

- Read, modify only klm's own rows, write back. Never rewrite the file wholesale.
- Back up before writing.
- The KiCad config path is **version-pinned** (`~/.config/kicad/9.0/…`). klm discovers the
  installed KiCad version rather than hardcoding it, and registers into the version it finds;
  registering into several versions is explicit (`klm register --kicad-version 8.0`).
- `klm register --check` reports what's missing without writing anything.

## 7. Migrations

The schema will change. `klm.store.migrations` holds numbered forward migrations applied inside a
transaction, with the schema version in `PRAGMA user_version`.

- Every migration is accompanied by a round-trip test on a fixture catalog.
- Before any migration, klm writes `catalog.db.pre-<version>.bak`.
- The YAML export format is versioned separately (`format_version` key) so an old export can be
  imported by a newer klm.
