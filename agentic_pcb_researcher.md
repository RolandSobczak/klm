# Architecture Documentation: AI Agent for PCB Library Automation

## 1. Project Goal

Build an autonomous AI agent that, starting from project requirements, searches for suitable
electronic components, verifies their availability at suppliers (TME, LCSC), and then
automatically builds and integrates complete libraries (Symbol, Footprint, 3D STEP model)
directly with the KiCad environment.

## 2. Technology Stack

* **Base language:** Python 3 (fully scripted, optimized for terminal/zsh)
* **CAD/EDA environment:** KiCad 8.0+
* **3D conversion:** FreeCAD (invoked headlessly via `freecadcmd`)
* **Dependencies and packages:** `JLC2KiCad_lib`, `subprocess`, `re`, `json`, `pathlib`
* **Supplier APIs:** TME API (REST/OAuth 2.0), LCSC (jlcsearch API)

## 3. System Architecture (Pipeline)

The agent's processing of a single component is divided into 5 asynchronous stages.

### Stage 1: Search and Acquisition (LLM + API)

The agent translates engineering requirements into search parameters.

* **TME:** Query the native REST API to find the primary candidate, retrieve the MPN
  (Manufacturer Part Number), and check stock levels.
* **LCSC:** Use the `jlcsearch` API (no authorization required) to fetch the exact component ID
  (e.g. `C2040`) for the EasyEDA variant.

### Stage 2: Download and 2D Conversion (EasyEDA -> KiCad)

Use a subprocess to run the `JLC2KiCad_lib` CLI tool. The tool parses the JSON structure and
generates S-expression files.

```python
import subprocess
from pathlib import Path

def run_jlc2kicad(lcsc_id: str, out_dir: Path):
    command = [
        "JLC2KiCad_lib", lcsc_id,
        "-dir", str(out_dir),
        "-symbol_lib", "ai_agent_symbols",
        "-footprint_lib", "ai_agent_footprints",
        "-model_dir", str(out_dir / "packages3d")
    ]
    subprocess.run(command, check=True, capture_output=True)
```

### Stage 3: 3D Solid Generation (OBJ -> STEP)

KiCad requires the `.step` format for advanced export and mechanical integration. The agent
launches a FreeCAD instance in the background to turn the surface mesh into a full solid.

**Helper script (`obj2step.py`):**

```python
import sys, Mesh, Part
mesh = Mesh.Mesh(sys.argv[1])
shape = Part.Shape()
shape.makeShapeFromMesh(mesh.Topology, 0.1)
Part.makeSolid(shape).exportStep(sys.argv[2])
```

**Invocation from the main code:**

```python
subprocess.run(["freecadcmd", "obj2step.py", "model.obj", "model.step"], check=True)
```

### Stage 4: Post-processing and Portability

The agent modifies the `.kicad_mod` file, stripping hard-coded paths and injecting an environment
variable (e.g. `${KICAD_USER_3DMOD}`). This guarantees library portability and the ability to
version it in Git.

```python
import re
from pathlib import Path

def inject_env_var_to_footprint(mod_path: Path, env_var="KICAD_USER_3DMOD"):
    content = mod_path.read_text(encoding="utf-8")
    pattern = re.compile(r'(\(model\s+")([^"]+?)("\s*\n)')

    def repl(m):
        filename = Path(m.group(2)).stem
        return f'{m.group(1)}${{{env_var}}}/{filename}.step{m.group(3)}'

    new_content, count = pattern.subn(repl, content)
    if count > 0:
        mod_path.write_text(new_content)
```

### Stage 5: Registration in the KiCad Ecosystem

Automatic modification of the user's configuration on Linux.

1. **Library tables:** Add entries to `~/.config/kicad/8.0/sym-lib-table` and `fp-lib-table`.
2. **Environment variables:** Inject the declared `${KICAD_USER_3DMOD}` variable into the
   `kicad_common.json` file.

```python
import json

def update_kicad_env_var(config_path: Path, var_name: str, var_value: str):
    data = json.loads(config_path.read_text())
    data.setdefault("environment", {}).setdefault("vars", {})[var_name] = var_value
    config_path.write_text(json.dumps(data, indent=2))
```

## 4. Environment Directory Tree

A structure optimized for integration in developer environments (e.g. editors supporting a
Neovim-style layout).

```text
ai-pcb-agent/
├── agent_core/
│   ├── main.py                 # LLM decision loop
│   ├── tme_client.py           # TME authorization and query handling
│   ├── lcsc_client.py          # jlcsearch queries
│   └── kicad_integrator.py     # Library table update module (Stage 5)
├── cad_tools/
│   ├── convert_3d.py           # Wrapper around the FreeCAD subprocess
│   └── scripts/
│       └── obj2step.py         # Script executed by freecadcmd
├── generated_libs/             # Target repository (Git)
│   ├── ai_agent_symbols.kicad_sym
│   ├── ai_agent_footprints.pretty/
│   └── packages3d/             # .step files
└── pyproject.toml              # Environment and dependency configuration
```

## 5. Future Extensions (Roadmap)

1. **Substitute management:** An algorithm that verifies pin-to-pin compatibility if the preferred
   integrated circuit goes out of stock at TME.
2. **Integration with a physical inventory system:** A module that exports the list of retrieved
   components to a format supported by thermal label printers. This will allow automatic printing
   of stickers (e.g. 14x14 mm) dedicated to physical, 3D-printed drawers for storing SMD parts,
   immediately after the agent approves the BOM.
