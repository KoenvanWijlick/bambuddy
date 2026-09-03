# Auto-Print Pipeline — implementation spec

One-click print flow: the user picks a **file**, a **filament type** and a **colour**
(optionally quality + layer height, both defaulted). Everything else — printer
choice, slicing profiles, orientation, slicing, queueing — happens automatically.

New page lives at `/print` ("Start a new print"). The existing `SliceModal` /
`PrintModal` expert flow is **left completely untouched**; this is an additional
surface, not a replacement.

Slicer: **Bambu Studio** sidecar (`bambu_studio_api_url`, default
`http://localhost:3001`). The `preferred_slicer` setting must resolve to
`bambu_studio`. Do not hard-code the URL — reuse the existing
`_resolve_slicer_api_url` pattern from `backend/app/api/routes/library.py`.

---

## Verified sidecar facts (measured against a live BambuStudio sidecar)

These were confirmed empirically. Build against them, do not re-derive.

`GET /profiles/bundled` → `{printer: [...], process: [...], filament: [...]}`

- `printer`: 70 entries, `{name, base_id}`. Names are
  `"Bambu Lab <model>"` and `"Bambu Lab <model> <d> nozzle"` for d in
  0.2/0.4/0.6/0.8. Example: `"Bambu Lab P1S 0.4 nozzle"`.
- `process`: 233 entries, `{name, base_id, compatible_printers: [...]}`.
  **Every** process profile carries a non-empty `compatible_printers`.
- `filament`: 1792 entries,
  `{name, base_id, compatible_printers, filament_type, filament_colour}`.

### Gotcha 1 — match on `compatible_printers`, NEVER on the profile name

A P1S has **zero** process profiles whose name contains `P1S`. Its 10 profiles
are all named `@BBL X1C` and are only discoverable through
`compatible_printers` containing `"Bambu Lab P1S 0.4 nozzle"`.

Any selector that parses the `@BBL <model>` suffix out of the name will
silently find nothing for P1S / P1P / X1E and similar. Always filter with:

```python
machine_key = f"Bambu Lab {model_long} {nozzle:.1f} nozzle"
candidates = [p for p in process if machine_key in (p.get("compatible_printers") or [])]
```

### Gotcha 2 — `filament_colour` is always null

All 1792 bundled filament profiles have `filament_colour: null`. Colour is
therefore **not** a preset-selection input. Colour drives:

1. which printer / AMS slot gets chosen, and
2. the queue item's `ams_mapping`.

Never try to find a "blue PETG" preset — pick the PETG preset and map it to a
tray that is loaded with blue.

### Gotcha 3 — `POST /profiles/resolve` takes a profile *object*

`{"category": "machine"|"process"|"filament", "profile": {...}}`. A bare name
string returns 400. For a bundled ("standard") profile, pass the same stub the
backend's `preset_resolver.py` already builds:

```json
{"category":"machine",
 "profile":{"name":"auto","inherits":"Bambu Lab X1 Carbon 0.4 nozzle",
            "from":"system","type":"machine"}}
```

This is how build volume is obtained — response contains
`printable_area: ["0x0","256x0","256x256","0x256"]`, `printable_height: "250"`,
`nozzle_diameter: ["0.4"]`. **Do not add a hand-maintained build-volume
table**; there is deliberately none in the codebase today.

### Gotcha 4 — a bed/plate type MUST be chosen, or the slice fails

Discovered by running a real slice. With the default plate, slicing PETG dies with:

```
Plate 1: Cool Plate does not support filament 1
  → exit code 195, "Filaments are not compatible with the plate type"
```

So the pipeline has to pick a plate type and inject it as `curr_bed_type` on the
**process** preset stub. Verified working:

```json
{"name":"0.20mm Standard @BBL X1C","inherits":"0.20mm Standard @BBL X1C",
 "from":"system","type":"process","curr_bed_type":"Textured PEI Plate"}
```

Compatibility is **data-driven — do not hard-code a filament→plate table.**
Resolve the chosen filament profile via `POST /profiles/resolve` and read its
`<plate>_plate_temp` keys. **A value of `0` means that plate is unsupported.**

| `curr_bed_type` value | temp key |
|---|---|
| `Cool Plate` | `cool_plate_temp` |
| `Engineering Plate` | `eng_plate_temp` |
| `High Temp Plate` | `hot_plate_temp` |
| `Textured PEI Plate` | `textured_plate_temp` |
| `Cool Plate (SuperTack)` | `supertack_plate_temp` |

Measured, showing there is no universally safe plate:

| Filament | cool | eng | hot | textured | supertack |
|---|---|---|---|---|---|
| PLA | 35 | **0** | 55 | 55 | 45 |
| PETG | **0** | 70 | 70 | 70 | 70 |

Selection rule: prefer the plate the printer actually reports over MQTT if it is
compatible; otherwise prefer `Textured PEI Plate`; otherwise the first plate
with a non-zero temp. If every plate is unsupported, fail with a user-facing
error naming the filament.

Add to `auto_preset_select.py`:

```python
async def select_bed_type(
    svc: SlicerApiService, *, filament_preset_name: str,
    printer_reported_bed_type: str | None = None,
) -> str        # a curr_bed_type value
```

`PresetSelection` gains a `bed_type: str` field, and the process preset stub
handed to the slicer must carry `curr_bed_type` set to it. `PresetChoice` in the
API schema gains `bed_type: str` so the UI can display it.

### Gotcha 5 — two things that will silently break a slice

Both are already handled correctly by the existing backend code; do not
"simplify" them away:

1. The model file part needs content type `model/stl` / `model/3mf`. Sending
   `application/octet-stream` gets a 400 "Invalid file type". Handled by
   `_guess_model_content_type` in `slicer_api.py:294`.
2. A preset stub's `name` must **equal** the bundled preset name, not a
   placeholder. With `name: "auto-process"` the CLI rejects the combination
   with "process not compatible with printer" (exit 239). Handled by
   `_resolve_standard` in `preset_resolver.py:254`.

### Verified end-to-end baseline

A 20×20×40mm L-bracket STL, X1C 0.4 nozzle, `0.20mm Standard @BBL X1C`,
`Bambu PETG Basic @BBL X1C`, Textured PEI Plate, `orient=true&arrange=true`:

- HTTP 200, `result.3mf` of ~51KB containing a real 290KB `Metadata/plate_1.gcode`
- `X-Print-Time-Seconds: 1746`, `X-Filament-Used-g: 4.43`, `X-Filament-Used-mm: 1474.77`
- The slicer log shows the orientation optimiser genuinely running (a cost table
  over 19 candidate orientations, choosing `best: 1.000000 -0.000000 -0.000000`),
  which confirms `orient=true` is not a no-op.
- No `plate_1.png` in the output — headless export omits thumbnails, which is why
  `inject_plate_thumbnails_if_missing` exists. Keep using it.

### Quality tiers actually available (0.4mm nozzle, X1C/P1S family)

| Tier | Layer heights |
|---|---|
| Extra Fine | 0.08 |
| High Quality | 0.08, 0.12, 0.16 |
| Fine | 0.12 |
| Optimal | 0.16 |
| **Standard** | **0.20** |
| Strength | 0.20 |
| Draft | 0.24 |
| Extra Draft | 0.28 |

Defaults for the page: quality `Standard`, layer height `0.20` — these resolve
to the real profile `"0.20mm Standard @BBL X1C"`.

Full tier set across all machines/nozzles: Standard, High Quality, Fine,
Extra Fine, Optimal, Balanced Quality, Balanced Strength, Strength, Steady,
Draft, Extra Draft.

33 distinct `filament_type` values exist (PLA, PETG, ABS, ASA, TPU, PA-CF, …).

---

## Reuse — do not reimplement

| Need | Existing thing |
|---|---|
| Sidecar HTTP client | `backend/app/services/slicer_api.py` (`SlicerApiService`) |
| Preset ref resolution | `backend/app/services/preset_resolver.py` (`resolve_preset_ref`, `PresetRef{source,id}`) |
| Slice + persist | `backend/app/api/routes/library.py` (`slice_and_persist`) |
| Background job pattern | `backend/app/services/slice_dispatch.py` — copy its structure |
| Auto-orient / arrange | `auto_orient` / `auto_arrange` on `SliceRequest`, `backend/app/schemas/slicer.py:173-193` |
| Filament preset from a spool | `backend/app/services/slicer_filament_resolver.py` |
| Model → canonical code | `backend/app/utils/printer_models.py` (`normalize_printer_model`, `PRINTER_MODEL_MAP`) |
| Live AMS tray state | `printer_manager` (`last_known_trays`), `backend/app/services/printer_manager.py` |
| Queue insert | `POST /api/v1/queue/`, `PrintQueueItemCreate` (`backend/app/schemas/print_queue.py:70`) |
| Printer auto-assign | `target_model` + `_find_idle_printer_for_model()`, `print_scheduler.py:2268` |
| Mesh preview (STL/3MF) | `frontend/src/components/ModelViewer.tsx` |
| Toolpath preview (gcode) | `frontend/src/components/GcodeToolpathViewer.tsx` |
| STL thumbnail | `backend/app/services/stl_thumbnail.py`, `plate_thumbnail.py` |

---

## Backend contract

### `backend/app/schemas/auto_print.py`

```python
AutoPrintStage = Literal[
    "pending", "uploading", "analysing", "printer_selected",
    "slicing", "queued", "failed",
]

class AutoPrintRequest(BaseModel):
    filament_type: str                      # e.g. "PETG"
    color_hex: str | None = None            # "#0A6CF5"; None = any loaded colour
    quality: str = "Standard"               # tier name, see table
    layer_height: float | None = 0.20
    printer_id: int | None = None           # explicit override; None = auto-pick
    auto_orient: bool = True
    auto_arrange: bool = True

class PresetChoice(BaseModel):
    printer: str          # e.g. "Bambu Lab P1S 0.4 nozzle"
    process: str          # e.g. "0.20mm Standard @BBL X1C"
    filament: str         # e.g. "Bambu PETG Basic @BBL X1C"

class PrinterChoice(BaseModel):
    id: int
    name: str
    model: str            # canonical short code, e.g. "P1S"
    nozzle_diameter: float
    reason: str           # human-readable why-this-printer, shown in the UI

class AutoPrintEstimate(BaseModel):
    print_time_seconds: int | None = None
    filament_used_g: float | None = None
    filament_used_mm: float | None = None

class AutoPrintFlow(BaseModel):
    id: int
    stage: AutoPrintStage
    stage_detail: str = ""       # e.g. "Slicing plate 1"
    progress: int = 0            # 0-100, drives the UI bar
    error: str | None = None
    library_file_id: int | None = None
    sliced_library_file_id: int | None = None
    queue_item_id: int | None = None
    printer: PrinterChoice | None = None
    presets: PresetChoice | None = None
    estimate: AutoPrintEstimate = AutoPrintEstimate()
    model_preview_url: str | None = None   # mesh, available from "analysing" on
    gcode_preview_url: str | None = None   # toolpath, available once "queued"
```

### `backend/app/services/auto_preset_select.py`

Pure, no DB. Caches `/profiles/bundled` in-process with a TTL.

```python
@dataclass(frozen=True)
class PresetSelection:
    printer: str
    process: str
    filament: str

class PresetSelectionError(Exception): ...

async def select_presets(
    svc: SlicerApiService,
    *,
    model_long: str,        # "Bambu Lab P1S" style long name
    nozzle_diameter: float, # 0.4
    filament_type: str,     # "PETG"
    quality: str,           # "Standard"
    layer_height: float | None,
) -> PresetSelection
```

Rules:
1. `machine_key = f"Bambu Lab {model_short_or_long} {nozzle_diameter:.1f} nozzle"`.
   Verify it exists in `printer[]`; if not, fall back to the nozzle-less
   `f"Bambu Lab {model}"` entry, else raise `PresetSelectionError`.
2. Process: filter `process[]` by `machine_key in compatible_printers`, then
   prefer exact `f"{layer_height:.2f}mm {quality} @..."`; else nearest layer
   height **within the requested tier**; else the tier-less nearest layer
   height; else raise.
3. Filament: filter `filament[]` by `machine_key in compatible_printers` and
   `filament_type == requested`. Prefer `Bambu <type> Basic`, then any
   `Bambu <type> …`, then `Generic <type>`, then first match. Raise if none.
4. Every returned name is used as `PresetRef(source="standard", id=<name>)`.

```python
async def get_build_volume(
    svc: SlicerApiService, *, machine_key: str
) -> tuple[float, float, float]   # (x, y, z) mm, from /profiles/resolve
```

```python
async def list_quality_options(
    svc: SlicerApiService, *, machine_key: str
) -> list[tuple[str, list[float]]]  # [(tier, sorted layer heights)]
```

### `backend/app/services/auto_printer_select.py`

```python
@dataclass(frozen=True)
class TrayMatch:
    printer_id: int
    ams_id: int
    tray_id: int
    global_tray_id: int     # value used in PrintQueueItemCreate.ams_mapping
    filament_type: str
    color_hex: str

@dataclass(frozen=True)
class PrinterSelection:
    printer: Printer
    nozzle_diameter: float
    tray: TrayMatch | None
    reason: str

class NoPrinterAvailableError(Exception):
    """Message must be user-facing and say *why* (no idle printer / colour not
    loaded / model too large for any bed)."""

async def select_printer(
    db: AsyncSession,
    *,
    filament_type: str,
    color_hex: str | None,
    model_size_mm: tuple[float, float, float] | None,
    explicit_printer_id: int | None = None,
) -> PrinterSelection
```

Ranking, highest first:
1. Reject any printer whose build volume cannot fit `model_size_mm`
   (get it via `get_build_volume`; skip the check when size is unknown).
2. Reject printers with no tray matching `filament_type` (+ `color_hex` when
   given). Colour match: nearest RGB distance, threshold ~40/255 — reuse
   whatever colour comparison already exists in the repo if there is one
   (check `print_scheduler._count_override_color_matches` and
   `frontend/src/components/filamentSwatchHelpers.ts`).
3. Prefer idle+connected over busy over offline.
4. Tie-break on shortest pending queue depth for that printer.

`reason` examples: `"Idle, blue PETG in AMS slot 2"`,
`"Shortest queue (1 pending), PETG loaded"`.

### `backend/app/services/auto_print_options.py`

Feeds the page's dropdowns so the user can only pick something printable.

```python
class LoadedFilament(BaseModel):
    filament_type: str
    color_hex: str
    color_name: str | None
    printer_ids: list[int]
    tray_count: int

async def list_loaded_filaments(db) -> list[LoadedFilament]
async def list_quality_tiers(db) -> list[dict]   # {tier, layer_heights[]}
async def default_options(db) -> dict            # {quality:"Standard", layer_height:0.20}
```

`list_loaded_filaments` reads live AMS tray state from `printer_manager` across
active printers, de-duplicating on `(filament_type, color_hex)`.

### `backend/app/services/auto_print_flow.py`

In-memory registry + background worker, structured exactly like
`slice_dispatch.py` (same dataclass/registry/`asyncio.create_task` shape).

```python
async def start_flow(
    db_session_factory, *, upload: UploadFile, request: AutoPrintRequest,
    current_user: User | None,
) -> int          # flow id

def get_flow(flow_id: int) -> AutoPrintFlow | None
```

Worker stages, updating `stage`/`progress` as it goes:

1. `uploading` — persist the upload as a `LibraryFile` (reuse the existing
   library upload service; do not re-implement storage-path logic).
2. `analysing` — if the file is already `.gcode` / `.gcode.3mf`, skip to step 6.
   Otherwise compute the mesh bounding box with `trimesh` (already a dependency)
   and generate the mesh preview via the existing thumbnail service.
3. `printer_selected` — `select_printer(...)`, then `select_presets(...)`.
4. `slicing` — call the existing slice-and-persist path with
   `auto_orient=True`, `auto_arrange=True` and the three chosen presets.
   Mirror progress from the sidecar's `/slice/progress/{requestId}`.
5. Record `print_time_seconds` / `filament_used_g` / `filament_used_mm` onto
   the flow's `estimate`.
6. `queued` — create the queue item via the existing queue-create service with
   `printer_id` = chosen printer and `ams_mapping` derived from the matched
   tray. Store `queue_item_id`.

Any exception → `stage="failed"` with a user-facing `error`. Never leak a
traceback into `error`.

### `backend/app/api/routes/auto_print.py`

```
POST /api/v1/auto-print/            multipart: file + AutoPrintRequest fields
                                    -> 202 {"id": int, "stage": "pending"}
GET  /api/v1/auto-print/{flow_id}   -> AutoPrintFlow
GET  /api/v1/auto-print/options     -> {filaments: [...], quality_tiers: [...],
                                        defaults: {...}}
```

Permission gate: match what `POST /library/files/{id}/slice` uses
(`Permission.LIBRARY_UPLOAD`). Register the router where the others are
registered.

### `backend/app/services/mock_printer_state.py` — dev only

Gated strictly on env `BAMBUDDY_MOCK_PRINTER_STATE=1`. When enabled, makes
`printer_manager` report a canned IDLE state plus AMS trays for any printer
row, so the pipeline can be exercised with no hardware. Must be a no-op — and
ideally not even imported into the request path — when the env var is absent.
This is a test harness, not a product feature: keep it in one module and do
not thread mock branches through production code.

---

## Frontend contract

### `frontend/src/pages/PrintPage.tsx`, route `/print`

Two-column layout, matching the approved design:

**Left column**
- `Start a new print` heading + subtitle
  ("Upload a file, choose your filament, adjust settings and start printing.")
- Dropzone: "Choose a file or drag and drop", `Supports .gcode, .3mf, .stl, .obj, .amf`,
  a `Choose File` button, and a "No file selected" / filename line.
- **Filament** section — "Select the filament type and color you are using."
  Two controls side by side: filament **type** select, and a **Color** select
  showing a colour swatch + name. Both populated from
  `GET /auto-print/options` (i.e. only what is actually loaded in the fleet).
- **Print Options** — `Quality` select and `Layer Height` select, defaulted from
  the options endpoint (`Standard` / `0.20 mm`).
- **Advanced Settings** — a `Collapsible`; put the `auto_orient` / `auto_arrange`
  toggles and the printer override here. Collapsed by default.
- Primary full-width `Print` button with a play icon.

**Right column**
- **Print Summary** card: preview viewport on top, then rows —
  File, Filament, Color, Estimated Print Time, Filament Usage, Layer Height,
  Printer. Each shows `—` until known, then fills in as the flow progresses.
  Preview shows `ModelViewer` once the file is uploaded, and swaps to
  `GcodeToolpathViewer` once slicing completes.
- **Printer Status** card: the auto-chosen printer with its online/offline
  state, model, and the `reason` string from `PrinterChoice`.

Behaviour: clicking `Print` POSTs to `/auto-print/`, then polls
`GET /auto-print/{id}` (~1 Hz) and drives a stage indicator through
upload → analyse → printer → slice → queued. On `queued`, show a success state
linking to the Print Queue. On `failed`, show `error` inline; the form stays
filled so the user can retry.

The queue item is created as a normal pending item — the existing scheduler
starts it when the printer is idle. Do **not** add a bypass that starts prints
directly.

### Styling

Tailwind with the existing `bambu-*` palette (`bambu-dark`, `bambu-dark-secondary`,
`bambu-dark-tertiary`, `bambu-card`, `bambu-green`). Reuse `Card`/`CardHeader`/
`CardContent`, `Button`, `Toggle`, `Collapsible` from `frontend/src/components/`.
There is no shared `Select` primitive — style native `<select>` inline, following
`SliceModal.tsx`'s existing pattern (`focus:ring-1 focus:ring-bambu-green`).

### Wiring

- `frontend/src/api/client.ts` — add interfaces + `api.startAutoPrint(formData)`,
  `api.getAutoPrintFlow(id)`, `api.getAutoPrintOptions()`. Multipart bypasses
  `request()` and builds `FormData` directly, like `api.uploadLibraryFile`.
- `frontend/src/App.tsx` — `<Route path="print" element={<PrintPage />} />`
  inside the `Layout`-wrapped block.
- `frontend/src/components/Layout.tsx` — add to `defaultNavItems`:
  `{ id: 'print', to: '/print', icon: Printer /* pick a distinct lucide icon */,
     labelKey: 'nav.print' }`, placed first.
- i18n — `nav.print` plus a `print.*` namespace in
  `frontend/src/i18n/locales/en.ts`, **and the same keys in all 13 other
  locales**. `frontend/scripts/check-i18n-parity.mjs` runs inside
  `npm run test:run` and fails the build on any missing key or placeholder
  mismatch.

---

## Local test environment (already running)

- BambuStudio sidecar: `http://localhost:3001` (`docker compose --profile bambu
  up -d bambu-studio-api` from `slicer-api/`). Healthy, version reports
  `"unknown"` — that is expected for the BambuStudio images.
- OrcaSlicer sidecar also up on `http://localhost:3003` (not the one we use).
- Backend: container `bambuddy-dev`, host networking, port 8000, with this
  worktree's `backend/` bind-mounted over `/app/backend`. Restart with
  `docker restart bambuddy-dev` to pick up Python changes (there is no
  `--reload`).
- Auth is **off** by default locally, so no login is needed.
- Frontend dev server: `cd frontend && npm run dev` → `http://localhost:5173`,
  proxying `/api` to `:8000`.
- Health checks: `GET /health` and `GET /api/v1/system/health` (note: there is
  no `/api/v1/health`).

### Tests

- Backend: `pytest backend/tests/...` — the suite redirects `DATABASE_URL` to a
  throwaway SQLite file, so it never touches dev data. Use the existing
  `printer_factory` and `mock_printer_manager` fixtures from
  `backend/tests/conftest.py` rather than inventing new printer fakes.
- Frontend: `cd frontend && npx tsc && npm run lint && npm run test:run`.
