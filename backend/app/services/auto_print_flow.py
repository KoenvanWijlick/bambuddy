"""In-memory background dispatcher for the auto-print pipeline.

Structured deliberately like ``slice_dispatch.py``: an in-memory registry
keyed by an auto-incrementing int id, one ``asyncio.Task`` per run, and a
mutable dataclass the polling endpoint reads a snapshot of on every tick.
The auto-print flow is a strict superset of a slice job though — upload,
mesh analysis, printer selection, slicing, queueing — so unlike
``SliceDispatchService`` it doesn't hand the whole job to one opaque
callable; the worker below IS the job, stage by stage, mutating ``_FlowState``
as it goes so ``GET /auto-print/{id}`` always has something fresh to report.

Design choices worth calling out:

* The actual slicing step reuses ``slice_dispatch`` itself rather than
  calling ``slice_and_persist`` directly inline. That gets two things for
  free instead of reimplementing them: the sidecar's live ``/slice/progress``
  polling (wired through ``job_id`` — see ``library.py``'s
  ``slice_library_file``) and the existing HTTPException-to-job-error
  boundary. This flow's worker then just polls *that* job and mirrors its
  progress onto its own ``_FlowState``, the same way the frontend's
  ``SliceJobTrackerContext`` mirrors it onto a toast.
* Every DB write (`upload_file`, `slice_and_persist`, `add_to_queue`) is the
  existing, un-modified route handler function, called directly rather than
  through HTTP — they're plain `async def`s; their `Depends(...)` parameter
  defaults are simply never evaluated when called this way, so passing
  `current_user=`/`db=` explicitly is all that's needed to reuse them
  wholesale (storage-path logic, thumbnail extraction, queue-scope locking,
  budget checks, notifications, MQTT relay — all of it, verbatim).
* One DB session (`db`, from `db_session_factory`) is used for the
  upload/analyse/printer-select/queue stages, opened once and held for the
  life of the flow — same reasoning as the slice route's `_run` closure
  (the *request's* session is gone by the time this background task runs).
  The slicing stage is the one exception: it hands `slice_dispatch` its own
  fresh session per the existing pattern, because that job runs as a
  logically separate concurrent task and `AsyncSession` is not safe to share
  across concurrent tasks.
* Queueing is no longer something the worker always does on its own (Round
  2's approval gate). By default the worker pauses at ``awaiting_approval``
  once slicing (or, for an already-sliced upload, printer selection) is
  done, and ``approve_flow``/``discard_flow`` — called from
  ``POST /{id}/approve`` and ``/discard``, not from this worker — resolve
  the pause later, from their own call stack, with their own fresh DB
  session. That's why the printer/tray chosen in stage 3 is captured onto
  ``_FlowState`` itself (``queue_printer_id``/``queue_ams_mapping``) rather
  than only living in a local variable, and why ``_queue_it`` — the one
  function that actually inserts the queue row — is written to be callable
  either from the worker or from ``approve_flow``.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import HTTPException, UploadFile

from backend.app.core.config import settings as app_settings
from backend.app.models.library import LibraryFile
from backend.app.models.user import User
from backend.app.schemas.auto_print import (
    AutoPrintEstimate,
    AutoPrintFlow,
    AutoPrintRequest,
    AutoPrintStage,
    PresetChoice,
    PrinterChoice,
)
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.schemas.slicer import PresetRef, SliceRequest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

_LIBRARY_API_PREFIX = f"{app_settings.api_prefix}/library"

# Same retention window as slice_dispatch — long enough that a polling client
# always sees the terminal state on its next ~1Hz tick, short enough not to
# leak memory across a long-running process.
_RETENTION_SECONDS = 30 * 60

# How often the slicing stage polls the underlying slice_dispatch job for a
# fresh progress snapshot. Matches SlicerApiService's own progress-poll
# cadence (see slicer_api.py's _PROGRESS_POLL_INTERVAL) — no point polling
# faster than the sidecar can possibly report something new.
_SLICE_POLL_INTERVAL_S = 1.0


class _FlowError(Exception):
    """Raised internally by the worker to fail the flow with a message that
    is safe to show the user as-is. Every place this is raised either wraps
    a caller-controlled ``HTTPException.detail`` (always a curated string in
    this codebase, never a traceback) or the message of
    ``NoPrinterAvailableError`` / ``PresetSelectionError``, both of which are
    documented to already be user-facing.
    """


class FlowStageConflictError(Exception):
    """Raised by ``approve_flow``/``discard_flow`` when the flow is not
    (or no longer) ``awaiting_approval``. Carries the stage actually
    observed so the route can build a specific 409 message. This also
    covers the "lost the race" case: two concurrent approve calls both
    pass the route's own pre-lock stage check, and the second one to
    acquire ``_FlowState.approval_lock`` finds the stage already moved to
    ``queued`` and raises this instead of queueing a second time — see
    ``approve_flow``'s docstring.
    """

    def __init__(self, stage: AutoPrintStage) -> None:
        self.stage = stage
        super().__init__(f"Auto-print flow is not awaiting approval (current stage: '{stage}').")


@dataclass(slots=True)
class _FlowState:
    """The mutable record for one auto-print run. `to_schema()` is the only
    way its contents leave this module — callers (the route, tests) never
    see or mutate this directly."""

    id: int
    owner_id: int | None = None
    stage: AutoPrintStage = "pending"
    stage_detail: str = ""
    progress: int = 0
    error: str | None = None
    library_file_id: int | None = None
    sliced_library_file_id: int | None = None
    queue_item_id: int | None = None
    printer: PrinterChoice | None = None
    presets: PresetChoice | None = None
    estimate: AutoPrintEstimate = field(default_factory=AutoPrintEstimate)
    model_preview_url: str | None = None
    gcode_preview_url: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None

    # --- Fields below this line are internal bookkeeping only — never
    # surfaced through `to_schema()` / the wire `AutoPrintFlow` shape. ---

    # The printer + AMS tray chosen in stage 3 (`printer_selected`), captured
    # here (rather than only living in the worker's local `printer_selection`
    # variable) so `approve_flow` can run the queue step later, from a
    # different call stack entirely, after the worker task that originally
    # selected them has already finished.
    queue_printer_id: int | None = None
    queue_ams_mapping: list[int] | None = None

    # Serialises `approve_flow`/`discard_flow` against each other and against
    # themselves for this one flow, so a concurrent double-approve (or an
    # approve racing a discard) can't both pass the "stage is
    # awaiting_approval" check before either has acted on it — the same
    # class of bug `_queue_it` was once fixed for (a stage transition
    # observed as "done" before the state it promises was actually true),
    # here guarded with a lock instead of ordering because the race is
    # between two independent callers, not one worker's own sequencing.
    approval_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def to_schema(self) -> AutoPrintFlow:
        return AutoPrintFlow(
            id=self.id,
            stage=self.stage,
            stage_detail=self.stage_detail,
            progress=self.progress,
            error=self.error,
            library_file_id=self.library_file_id,
            sliced_library_file_id=self.sliced_library_file_id,
            queue_item_id=self.queue_item_id,
            printer=self.printer,
            presets=self.presets,
            estimate=self.estimate,
            model_preview_url=self.model_preview_url,
            gcode_preview_url=self.gcode_preview_url,
        )


class AutoPrintDispatchService:
    """The registry + task supervisor. Structurally identical to
    ``SliceDispatchService``: a dict of state objects, an incrementing id
    counter under a lock, one task per run, and a retention sweep on every
    new registration."""

    def __init__(self) -> None:
        self._flows: dict[int, _FlowState] = {}
        self._next_id: int = 1
        self._lock = asyncio.Lock()
        self._tasks: dict[int, asyncio.Task] = {}

    async def start(
        self,
        *,
        db_session_factory: async_sessionmaker[AsyncSession],
        upload_bytes: bytes,
        upload_filename: str,
        request: AutoPrintRequest,
        owner_id: int | None,
    ) -> _FlowState:
        async with self._lock:
            flow = _FlowState(id=self._next_id, owner_id=owner_id)
            self._next_id += 1
            self._flows[flow.id] = flow
            self._sweep_locked()

        task = asyncio.create_task(
            _run_flow(
                flow,
                db_session_factory=db_session_factory,
                upload_bytes=upload_bytes,
                upload_filename=upload_filename,
                request=request,
            ),
            name=f"auto-print-flow-{flow.id}",
        )
        self._tasks[flow.id] = task
        return flow

    def get(self, flow_id: int) -> _FlowState | None:
        return self._flows.get(flow_id)

    def _sweep_locked(self) -> None:
        now = datetime.now(timezone.utc)
        stale_ids = [
            fid
            for fid, flow in self._flows.items()
            if flow.stage in ("queued", "failed", "discarded")
            and flow.completed_at is not None
            and (now - flow.completed_at).total_seconds() > _RETENTION_SECONDS
        ]
        for fid in stale_ids:
            self._flows.pop(fid, None)


# Module-level singleton, mirroring `slice_dispatch` — no lifespan hook needed
# since (like SliceDispatchService) it holds no resources beyond its own dict.
auto_print_dispatch = AutoPrintDispatchService()


async def start_flow(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    upload: UploadFile,
    request: AutoPrintRequest,
    current_user: User | None,
) -> int:
    """Read the upload into memory and kick off the background worker.

    The upload is read here, synchronously, before returning — mirroring
    `slice_library_file`'s `model_bytes = src_path.read_bytes()`: the
    request (and the `UploadFile`'s underlying temp file / connection) does
    not reliably outlive the route handler's return, so anything the
    background task needs from it has to be captured up front.
    """
    upload_bytes = await upload.read()
    upload_filename = upload.filename or "upload"
    owner_id = current_user.id if current_user else None

    flow = await auto_print_dispatch.start(
        db_session_factory=db_session_factory,
        upload_bytes=upload_bytes,
        upload_filename=upload_filename,
        request=request,
        owner_id=owner_id,
    )
    return flow.id


def get_flow(flow_id: int) -> AutoPrintFlow | None:
    flow = auto_print_dispatch.get(flow_id)
    return flow.to_schema() if flow is not None else None


def get_flow_owner_id(flow_id: int) -> int | None:
    """The user id that started this flow (`None` for an API-key / auth-
    disabled caller). Used by the route for the same per-row ownership
    scoping `slice_jobs.py` applies to slice-job polling — kept as a
    separate accessor rather than widening `AutoPrintFlow` itself, since
    ownership is an auth concern the wire schema has no reason to carry.
    """
    flow = auto_print_dispatch.get(flow_id)
    return flow.owner_id if flow is not None else None


async def approve_flow(db_session_factory: async_sessionmaker[AsyncSession], flow_id: int) -> AutoPrintFlow | None:
    """Approve a flow paused at ``awaiting_approval``, running the same
    queue step the worker itself uses (``_queue_it``) and landing it on
    ``queued``. Returns ``None`` if the flow id is unknown/expired (the
    route turns that into a 404, same as ``get_flow``); raises
    ``FlowStageConflictError`` if the flow is not currently
    ``awaiting_approval`` (the route turns that into a 409).

    The stage check is deliberately repeated *inside*
    ``flow.approval_lock`` rather than trusted from the route's own
    pre-lock check: two concurrent ``POST /approve`` calls can both observe
    ``awaiting_approval`` before either gets here, so only re-checking under
    the lock — and only the first caller through it ever calling
    ``_queue_it`` — prevents a second queue item. This is the same
    "don't mark a transition done before the awaited work behind it is
    actually done" discipline ``_queue_it`` already applies to the worker's
    own single-threaded sequencing, applied here to a genuine multi-caller
    race instead.

    Opens its own fresh DB session (via ``db_session_factory``, the same
    ``async_session`` the route passes to ``start_flow``) rather than reusing
    the worker's — that session was closed when ``_run_flow`` exited back in
    the ``awaiting_approval`` transition, and may be running in a different
    call stack anyway (a real HTTP request, not the background task).
    """
    flow = auto_print_dispatch.get(flow_id)
    if flow is None:
        return None

    from backend.app.api.routes.print_queue import add_to_queue

    async with flow.approval_lock:
        if flow.stage != "awaiting_approval":
            raise FlowStageConflictError(flow.stage)

        assert flow.sliced_library_file_id is not None, "awaiting_approval guarantees a sliced file"

        async with db_session_factory() as db:
            user = await db.get(User, flow.owner_id) if flow.owner_id is not None else None
            try:
                await _queue_it(
                    flow,
                    db=db,
                    add_to_queue=add_to_queue,
                    user=user,
                    library_file_id=flow.sliced_library_file_id,
                )
            except _FlowError as exc:
                # Mirrors `_run_flow`'s own catch of `_FlowError`: a queueing
                # failure here is a normal, user-facing failure of the flow,
                # not an exception the HTTP layer should see — `approve`
                # still returns 200 with `stage='failed'` and a readable
                # `error`, exactly as if this had failed during the original
                # worker run.
                logger.info("Auto-print flow %s failed during approval: %s", flow_id, exc)
                flow.stage = "failed"
                flow.error = str(exc)
            flow.completed_at = datetime.now(timezone.utc)

    return flow.to_schema()


async def discard_flow(flow_id: int) -> AutoPrintFlow | None:
    """Discard a flow paused at ``awaiting_approval``: lands on
    ``discarded`` and queues nothing. The uploaded file and the sliced
    result are left exactly as they are in the library — only the queue
    step is skipped, on purpose (see docs/auto-print-pipeline-spec.md
    Round 2: "the uploaded file and the sliced result stay in the
    library"). Returns ``None``/raises ``FlowStageConflictError`` on the
    same terms as ``approve_flow``, including the same lock-then-recheck
    discipline so a discard racing an approve can't leave the flow in an
    inconsistent state — whichever call acquires ``approval_lock`` first
    decides the outcome, and the second sees the already-moved stage.
    """
    flow = auto_print_dispatch.get(flow_id)
    if flow is None:
        return None

    async with flow.approval_lock:
        if flow.stage != "awaiting_approval":
            raise FlowStageConflictError(flow.stage)
        flow.stage = "discarded"
        flow.stage_detail = ""
        flow.completed_at = datetime.now(timezone.utc)

    return flow.to_schema()


async def _resolve_bambu_studio_api_url(db: AsyncSession) -> str:
    """The Bambu Studio sidecar URL, enforcing that it's actually the
    configured slicer.

    Auto-print only ever targets Bambu Studio (see docs/auto-print-pipeline-
    spec.md) — unlike the SliceModal, which lets the user pick either
    sidecar per-slice, there is no UI here for the user to choose OrcaSlicer.
    Reuses `slicer_presets._resolve_slicer_api_url` for the actual URL
    resolution (per-install override, then the env default) rather than
    re-deriving it; the `preferred_slicer` gate on top of that is specific
    to this feature.
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.api.routes.slicer_presets import _resolve_slicer_api_url

    preferred = (await get_setting(db, "preferred_slicer")) or "bambu_studio"
    if preferred != "bambu_studio":
        raise _FlowError(
            "Auto-print requires Bambu Studio as the preferred slicer. "
            f"Change 'Preferred slicer' to Bambu Studio in Settings → Slicer (currently: {preferred})."
        )
    api_url = await _resolve_slicer_api_url(db)
    if not api_url:
        raise _FlowError("No Bambu Studio slicer sidecar is configured. Set a sidecar URL in Settings → Slicer.")
    return api_url


def _detail_to_message(detail: object) -> str:
    """`HTTPException.detail` is a plain curated string everywhere this flow
    calls into, but the type is `Any` — coerce defensively rather than let a
    non-string detail (or, worse, `repr()` of something ugly) reach the
    user."""
    return detail if isinstance(detail, str) else str(detail)


async def _run_flow(
    flow: _FlowState,
    *,
    db_session_factory: async_sessionmaker[AsyncSession],
    upload_bytes: bytes,
    upload_filename: str,
    request: AutoPrintRequest,
) -> None:
    async with db_session_factory() as db:
        try:
            await _run_flow_stages(
                flow,
                db=db,
                db_session_factory=db_session_factory,
                upload_bytes=upload_bytes,
                upload_filename=upload_filename,
                request=request,
            )
        except _FlowError as exc:
            logger.info("Auto-print flow %s failed: %s", flow.id, exc)
            flow.stage = "failed"
            flow.error = str(exc)
        except Exception:
            logger.exception("Auto-print flow %s failed unexpectedly", flow.id)
            flow.stage = "failed"
            flow.error = "Unexpected error while starting the print. Check the server log for details."
        finally:
            flow.completed_at = datetime.now(timezone.utc)


async def _run_flow_stages(
    flow: _FlowState,
    *,
    db: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    upload_bytes: bytes,
    upload_filename: str,
    request: AutoPrintRequest,
) -> None:
    from backend.app.api.routes.library import to_absolute_path, upload_file
    from backend.app.api.routes.print_queue import add_to_queue

    user = await db.get(User, flow.owner_id) if flow.owner_id is not None else None

    # --- Stage 1: uploading ---------------------------------------------
    flow.stage = "uploading"
    flow.stage_detail = "Uploading file"
    flow.progress = 5

    synthetic_upload = UploadFile(file=io.BytesIO(upload_bytes), filename=upload_filename, size=len(upload_bytes))
    try:
        upload_response = await upload_file(
            file=synthetic_upload,
            folder_id=None,
            generate_stl_thumbnails=True,
            db=db,
            current_user=user,
        )
    except HTTPException as exc:
        raise _FlowError(_detail_to_message(exc.detail)) from exc

    flow.library_file_id = upload_response.id
    flow.progress = 15

    result = await db.execute(LibraryFile.active().where(LibraryFile.id == upload_response.id))
    lib_file = result.scalar_one_or_none()
    if lib_file is None:
        raise _FlowError("Uploaded file could not be found — please try again.")

    # --- Stage 2: analysing ----------------------------------------------
    flow.stage = "analysing"
    flow.stage_detail = "Reading model"
    flow.progress = 20
    flow.model_preview_url = f"{_LIBRARY_API_PREFIX}/files/{lib_file.id}/download"

    # A sliced file (gcode, or a 3MF with embedded gcode) needs neither a
    # bounding-box measurement nor a trip through the slicer — it goes
    # straight to printer selection and then queueing with the upload
    # itself as the thing to print. `file_type` (set by `classify_file_type`
    # during upload, which sniffs the zip rather than trusting the
    # extension) is the source of truth here, matching how the rest of the
    # library routes tell a sliced 3MF apart from a plain one.
    already_sliced = lib_file.file_type in ("gcode", "gcode.3mf")

    model_size_mm: tuple[float, float, float] | None = None
    abs_path = to_absolute_path(lib_file.file_path)
    if abs_path is None or not abs_path.exists():
        raise _FlowError("Uploaded file is missing on disk.")

    if already_sliced:
        flow.gcode_preview_url = f"{_LIBRARY_API_PREFIX}/files/{lib_file.id}/gcode"
        meta = lib_file.file_metadata or {}
        flow.estimate = AutoPrintEstimate(
            print_time_seconds=meta.get("print_time_seconds"),
            filament_used_g=meta.get("filament_used_grams") or meta.get("filament_used_g"),
            filament_used_mm=meta.get("filament_used_mm"),
        )
    else:
        try:
            import trimesh

            mesh = trimesh.load(str(abs_path), force="mesh")
            if mesh is not None and getattr(mesh, "vertices", None) is not None and len(mesh.vertices) > 0:
                extents = mesh.extents
                model_size_mm = (float(extents[0]), float(extents[1]), float(extents[2]))
        except Exception as exc:  # noqa: BLE001 — best-effort; printer selection skips the fit check when None
            logger.warning("Auto-print flow %s: could not read mesh bounding box: %s", flow.id, exc)
            model_size_mm = None

    flow.progress = 30

    # --- Stage 3: printer_selected -----------------------------------------
    flow.stage = "printer_selected"
    flow.stage_detail = "Choosing a printer"
    flow.progress = 35

    from backend.app.services.auto_printer_select import NoPrinterAvailableError, model_long_name, select_printer

    try:
        printer_selection = await select_printer(
            db,
            filament_type=request.filament_type,
            color_hex=request.color_hex,
            model_size_mm=model_size_mm,
            explicit_printer_id=request.printer_id,
        )
    except NoPrinterAvailableError as exc:
        raise _FlowError(str(exc)) from exc

    flow.printer = PrinterChoice(
        id=printer_selection.printer.id,
        name=printer_selection.printer.name,
        model=printer_selection.printer.model or "unknown",
        nozzle_diameter=printer_selection.nozzle_diameter,
        reason=printer_selection.reason,
    )
    # Captured onto the flow itself (not just the local `printer_selection`)
    # so a later, out-of-process `approve_flow` call can run `_queue_it`
    # without needing this worker's own call stack — see `_FlowState`'s
    # field comments.
    flow.queue_printer_id = printer_selection.printer.id
    flow.queue_ams_mapping = [printer_selection.tray.global_tray_id] if printer_selection.tray is not None else None
    flow.progress = 45

    if already_sliced:
        # Nothing left to slice — the upload IS the printable file.
        flow.sliced_library_file_id = lib_file.id
        await _reach_approval_or_queue(
            flow,
            db=db,
            add_to_queue=add_to_queue,
            user=user,
            library_file_id=lib_file.id,
            require_approval=request.require_approval,
        )
        return

    # --- Stage 4/5: slicing -------------------------------------------------
    flow.stage = "slicing"
    flow.stage_detail = "Choosing slicer presets"
    flow.progress = 50

    from backend.app.services.auto_preset_select import PresetSelectionError, select_presets
    from backend.app.services.slicer_api import SlicerApiError, SlicerApiService

    api_url = await _resolve_bambu_studio_api_url(db)

    # `auto_printer_select.model_long_name` is the correct short->long name
    # mapping for the sidecar's bundled profiles — NOT a plain "Bambu Lab "
    # prefix (it corrects spelling quirks like "Bambu Lab A1 mini", lower-
    # case "mini", which the naive prefix would get wrong). Falls back to
    # the raw short code for an unrecognised model, matching how
    # `select_presets`'s own `_machine_key` degrades for an unknown name.
    model_long = model_long_name(printer_selection.printer.model) or printer_selection.printer.model or ""

    try:
        async with SlicerApiService(base_url=api_url) as svc:
            # No MQTT telemetry in this codebase reports which physical
            # plate is installed (checked: bambu_mqtt.py's only "bed_type"
            # is the literal string "auto" sent as part of a print-start
            # command, never something read back from the printer) — so
            # there is nothing truthful to pass as printer_reported_bed_type
            # other than None. select_presets/select_bed_type falls back to
            # Textured PEI Plate / the first compatible plate on their own.
            preset_selection = await select_presets(
                svc,
                model_long=model_long,
                nozzle_diameter=printer_selection.nozzle_diameter,
                filament_type=request.filament_type,
                quality=request.quality,
                layer_height=request.layer_height,
                printer_reported_bed_type=None,
            )
    except (PresetSelectionError, SlicerApiError) as exc:
        raise _FlowError(str(exc)) from exc

    # Brim (Round 2): an explicit request field, not a preset-selection
    # concern like the other three names above — `request.brim` is a plain
    # per-print toggle, so it maps straight to a `SliceRequest` override
    # rather than going through `auto_preset_select` at all.
    #
    # `False` maps to `"auto_brim"` — the profile's own default — deliberately,
    # NOT to `"no_brim"`. The toggle's job is to force an inner+outer brim;
    # switching it off means "stop forcing it", not "forbid a brim entirely".
    # Those differ in a way that can ruin a print: `no_brim` would strip the
    # adhesion aid from a tall, small-footprint part that `auto_brim` would
    # have given one to, so an off-toggle would be silently causing failed
    # prints. A user who genuinely wants no brim at all can say so through
    # `SliceRequest.brim_type` on the expert slice path.
    brim_type = "outer_and_inner" if request.brim else "auto_brim"
    brim_width = request.brim_width if request.brim else None
    brim_label = f"Inner + outer, {request.brim_width:g} mm" if request.brim else "Automatic"

    flow.presets = PresetChoice(
        printer=preset_selection.printer,
        process=preset_selection.process,
        filament=preset_selection.filament,
        bed_type=preset_selection.bed_type,
        brim=brim_label,
    )
    flow.stage_detail = "Slicing"
    flow.progress = 55

    slice_request = SliceRequest(
        printer_preset=PresetRef(source="standard", id=preset_selection.printer),
        process_preset=PresetRef(source="standard", id=preset_selection.process),
        filament_presets=[PresetRef(source="standard", id=preset_selection.filament)],
        bed_type=preset_selection.bed_type,
        brim_type=brim_type,
        brim_width=brim_width,
        auto_orient=request.auto_orient,
        auto_arrange=request.auto_arrange,
    )

    slice_result = await _run_slice(
        flow,
        db_session_factory=db_session_factory,
        model_bytes=abs_path.read_bytes(),
        model_filename=lib_file.filename,
        folder_id=lib_file.folder_id,
        slice_request=slice_request,
        owner_id=flow.owner_id,
    )

    flow.sliced_library_file_id = slice_result.get("library_file_id")
    if flow.sliced_library_file_id is not None:
        flow.gcode_preview_url = f"{_LIBRARY_API_PREFIX}/files/{flow.sliced_library_file_id}/gcode"
    flow.estimate = AutoPrintEstimate(
        print_time_seconds=slice_result.get("print_time_seconds"),
        filament_used_g=slice_result.get("filament_used_g"),
        filament_used_mm=slice_result.get("filament_used_mm"),
    )
    flow.progress = 85

    # --- Stage 6: awaiting_approval, or straight through to queued ----------
    if flow.sliced_library_file_id is None:
        raise _FlowError("Slicing finished without producing a file — please try again.")

    await _reach_approval_or_queue(
        flow,
        db=db,
        add_to_queue=add_to_queue,
        user=user,
        library_file_id=flow.sliced_library_file_id,
        require_approval=request.require_approval,
    )


async def _run_slice(
    flow: _FlowState,
    *,
    db_session_factory: async_sessionmaker[AsyncSession],
    model_bytes: bytes,
    model_filename: str,
    folder_id: int | None,
    slice_request: SliceRequest,
    owner_id: int | None,
) -> dict:
    """Run the slice via the existing `slice_dispatch` job registry, mirroring
    its live progress onto `flow` as it goes.

    Reusing `slice_dispatch` (rather than calling `slice_and_persist`
    in-line) is what gets real `/slice/progress/{requestId}` mirroring for
    free: passing a `job_id` into `slice_and_persist` is what makes it wire a
    progress callback to `slice_dispatch.set_progress(job_id, ...)` in the
    first place (see `library.py`'s `_run_slicer_with_fallback`) — that
    callback is a no-op for any id `slice_dispatch` doesn't itself know
    about, so the job has to actually be registered there.
    """
    from backend.app.services.slice_dispatch import http_exception_to_job_error, slice_dispatch

    async def _run(job_id: int) -> dict:
        from backend.app.api.routes.library import slice_and_persist

        async with db_session_factory() as slice_db:
            try:
                response = await slice_and_persist(
                    slice_db,
                    model_bytes=model_bytes,
                    model_filename=model_filename,
                    folder_id=folder_id,
                    extra_metadata={"auto_print_flow_id": flow.id},
                    request=slice_request,
                    current_user_id=owner_id,
                    job_id=job_id,
                )
            except HTTPException as exc:
                raise http_exception_to_job_error(exc) from exc
        return response.model_dump()

    job = await slice_dispatch.enqueue(
        kind="library_file",
        source_id=flow.library_file_id or 0,
        source_name=model_filename,
        owner_id=owner_id,
        run=_run,
    )

    while True:
        current = slice_dispatch.get(job.id)
        if current is None:
            raise _FlowError("The slicing job was lost — please try again.")

        if current.progress:
            stage_name = current.progress.get("stage")
            total_percent = current.progress.get("total_percent")
            if stage_name:
                flow.stage_detail = str(stage_name)
            if isinstance(total_percent, (int, float)):
                # Slicing owns the 55-85% band of the overall flow bar.
                flow.progress = 55 + round(max(0.0, min(100.0, float(total_percent))) * 0.30)

        if current.status == "completed":
            return current.result or {}
        if current.status == "failed":
            raise _FlowError(current.error_detail or "Slicing failed.")

        await asyncio.sleep(_SLICE_POLL_INTERVAL_S)


async def _reach_approval_or_queue(
    flow: _FlowState,
    *,
    db: AsyncSession,
    add_to_queue: Callable,
    user: User | None,
    library_file_id: int,
    require_approval: bool,
) -> None:
    """The fork at the end of stage 5 (or, for an already-sliced upload,
    straight after stage 3): pause at ``awaiting_approval`` for the caller
    to resolve via `POST /{id}/approve` or `/discard` (the default), or —
    when `AutoPrintRequest.require_approval` is `False` — queue immediately,
    reproducing the pre-approval-gate one-shot behaviour.

    By the time this runs, `flow.sliced_library_file_id`, `gcode_preview_url`
    (when there is one to slice — see the already-sliced branch in
    `_run_flow_stages`) and `estimate` are already populated by the caller;
    that's the whole point of pausing here rather than earlier. Nothing is
    queued on the pause path: `queue_item_id` stays `None` until a later,
    separate `approve_flow` call runs `_queue_it`.
    """
    if not require_approval:
        await _queue_it(flow, db=db, add_to_queue=add_to_queue, user=user, library_file_id=library_file_id)
        return

    flow.stage_detail = "Review the sliced result before it's queued"
    flow.progress = 85
    flow.stage = "awaiting_approval"


async def _queue_it(
    flow: _FlowState,
    *,
    db: AsyncSession,
    add_to_queue: Callable,
    user: User | None,
    library_file_id: int,
) -> None:
    """Insert the print-queue item. Called either straight from the worker
    (``require_approval=False``, or an already-sliced upload with approval
    off) or later, out of process, from ``approve_flow``.

    `flow.queue_printer_id` / `flow.queue_ams_mapping` — not a
    `PrinterSelection` object — are what this reads: they're captured onto
    the flow back in stage 3 specifically so this function has something to
    read regardless of which call stack it's running on (see `_FlowState`'s
    field comments).

    `stage` is set to "queued" only once the insert has actually succeeded
    (see the assignment below) — never before `add_to_queue` is awaited.
    "queued" is the flow's terminal success state (the frontend shows it as
    "done, linking to the Print Queue"), so a poller must never be able to
    observe it with `queue_item_id` still `None`. Setting it early here,
    matching the other stages' "name the stage, then do the work" pattern,
    would leave exactly that window open across the `await` below — this is
    the exact bug class `approve_flow`'s lock now also guards against for
    the multi-caller case.
    """
    flow.stage_detail = "Adding to print queue"
    flow.progress = 90

    assert flow.queue_printer_id is not None, "queue_printer_id is set in stage 3, before this can ever run"

    queue_request = PrintQueueItemCreate(
        printer_id=flow.queue_printer_id,
        library_file_id=library_file_id,
        ams_mapping=flow.queue_ams_mapping,
    )

    try:
        queue_response = await add_to_queue(queue_request, db=db, current_user=user)
    except HTTPException as exc:
        raise _FlowError(_detail_to_message(exc.detail)) from exc

    flow.queue_item_id = queue_response.id
    flow.stage_detail = ""
    flow.progress = 100
    flow.stage = "queued"
