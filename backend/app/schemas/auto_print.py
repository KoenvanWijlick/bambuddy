"""Pydantic schemas for the auto-print pipeline ("Start a new print").

The auto-print page asks the user for exactly three things — a file, a
filament *type*, and a colour (quality/layer-height are defaulted) — and
turns that into a fully-queued print with no further input. Everything else
(which printer, which slicer presets, which AMS tray, the actual slice) is
derived server-side by `auto_print_flow.py` and the two selection services
it calls (`auto_preset_select.py`, `auto_printer_select.py`).

These schemas are the wire contract for that flow: the request body the
page POSTs, and the polled status object it reads back every ~1s while the
flow runs in the background. Kept in their own module (rather than folded
into `slicer.py` or `print_queue.py`) because this is a new, independent
surface — the existing SliceModal / PrintModal expert flow is untouched and
shares none of these shapes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Mirrors the stages `auto_print_flow.py`'s worker walks through, in order.
# "pending" is the state a flow is created in, before the background task's
# first tick has actually run — the polling client may see it for a moment
# on the very first GET. "failed" is terminal and can be reached from any
# other stage; every other stage is otherwise linear.
AutoPrintStage = Literal[
    "pending",
    "uploading",
    "analysing",
    "printer_selected",
    "slicing",
    "queued",
    "failed",
]


class AutoPrintRequest(BaseModel):
    """Body for `POST /api/v1/auto-print/` (sent as multipart form fields
    alongside the uploaded file — see the route module for why this isn't a
    single JSON body)."""

    filament_type: str = Field(..., description="e.g. 'PETG' — matched against bundled filament presets.")
    color_hex: str | None = Field(
        default=None,
        description="'#0A6CF5'; None means any loaded colour of `filament_type` is acceptable.",
    )
    quality: str = Field(default="Standard", description="Quality tier name — see the bundled process tiers.")
    layer_height: float | None = Field(default=0.20, description="mm. None defers entirely to `quality`.")
    printer_id: int | None = Field(default=None, description="Explicit printer override; None = auto-pick.")
    auto_orient: bool = True
    auto_arrange: bool = True


class PresetChoice(BaseModel):
    """The three bundled Bambu Studio profile names actually used to slice,
    plus the bed/plate type injected onto the process preset. Shown in the
    UI as "what auto-print picked for you"."""

    printer: str = Field(..., description="e.g. 'Bambu Lab P1S 0.4 nozzle'")
    process: str = Field(..., description="e.g. '0.20mm Standard @BBL X1C'")
    filament: str = Field(..., description="e.g. 'Bambu PETG Basic @BBL X1C'")
    # A plate/bed type MUST be chosen or the slice fails outright — see
    # docs/auto-print-pipeline-spec.md Gotcha 4 (a default "Cool Plate"
    # rejects PETG with "Filaments are not compatible with the plate type").
    # Surfaced here so the UI can show the user which plate the pipeline
    # assumed, since nothing else on the page asks them to pick one.
    bed_type: str = Field(
        ..., description="e.g. 'Textured PEI Plate' — injected as the process preset's curr_bed_type."
    )


class PrinterChoice(BaseModel):
    """The printer auto-selected for this print, and why."""

    id: int
    name: str
    model: str = Field(..., description="Canonical short code, e.g. 'P1S'.")
    nozzle_diameter: float
    reason: str = Field(..., description="Human-readable why-this-printer, shown in the UI.")


class AutoPrintEstimate(BaseModel):
    print_time_seconds: int | None = None
    filament_used_g: float | None = None
    filament_used_mm: float | None = None


class AutoPrintFlow(BaseModel):
    """The full state of one auto-print run, as returned by
    `GET /api/v1/auto-print/{flow_id}`. The frontend polls this at ~1Hz and
    drives a stage indicator off `stage`/`progress`, filling in the Print
    Summary card's rows as each field goes from `None` to a real value."""

    id: int
    stage: AutoPrintStage
    stage_detail: str = Field(default="", description="e.g. 'Slicing plate 1' — freeform, for display only.")
    progress: int = Field(default=0, ge=0, le=100, description="Drives the UI progress bar.")
    error: str | None = Field(
        default=None,
        description="User-facing message when stage='failed'. Never a traceback or internal detail.",
    )
    library_file_id: int | None = None
    sliced_library_file_id: int | None = None
    queue_item_id: int | None = None
    printer: PrinterChoice | None = None
    presets: PresetChoice | None = None
    estimate: AutoPrintEstimate = Field(default_factory=AutoPrintEstimate)
    model_preview_url: str | None = Field(default=None, description="Mesh preview URL, available from 'analysing' on.")
    gcode_preview_url: str | None = Field(default=None, description="Toolpath preview URL, available once 'queued'.")
