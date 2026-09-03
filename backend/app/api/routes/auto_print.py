"""The "Start a new print" one-click flow.

Five endpoints:

- ``POST /`` accepts the upload + the user's choices (file, filament type,
  colour, brim, ... — quality/layer-height/approval defaulted) as multipart
  form fields, and returns immediately with a flow id; the actual work
  (upload, analyse, pick a printer, slice, pause-for-approval) runs in the
  background via ``auto_print_flow.start_flow``.
- ``GET /{flow_id}`` is what the frontend polls at ~1Hz to drive the stage
  indicator and fill in the Print Summary card.
- ``POST /{flow_id}/approve`` and ``POST /{flow_id}/discard`` resolve a flow
  paused at ``stage='awaiting_approval'`` — see `auto_print_flow.py`'s
  ``approve_flow``/``discard_flow`` for the actual logic; this route is just
  the stage-conflict-to-409 / not-found-to-404 translation layer on top,
  plus the same ownership scoping as ``GET /{flow_id}``.
- ``GET /options`` feeds the page's dropdowns with only what the fleet can
  actually print right now.

Permission gate mirrors ``POST /library/files/{id}/slice``
(``Permission.LIBRARY_UPLOAD``) — this whole surface is "upload + slice +
queue" collapsed into one action, so the one upload-time permission covers
it; nothing here bypasses `POST /queue/`'s own validation, it's just called
directly instead of over HTTP (see `auto_print_flow.py`'s module docstring).
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import require_permission_if_auth_enabled
from backend.app.core.database import async_session, get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.schemas.auto_print import AutoPrintFlow, AutoPrintRequest
from backend.app.services.auto_print_flow import (
    FlowStageConflictError,
    approve_flow,
    discard_flow,
    get_flow,
    get_flow_owner_id,
    start_flow,
)
from backend.app.services.auto_print_options import default_options, list_loaded_filaments, list_quality_tiers

# Dev-only mock harness (see mock_printer_state.py's module docstring). The
# module is imported — and therefore does anything at all — only when the
# env var is actually set, so a production process where it's unset never
# even loads it. `install_mock_printer_state()` re-checks the same env var
# itself, so this stays a no-op even if some other module imports it
# unconditionally in the future.
if os.environ.get("BAMBUDDY_MOCK_PRINTER_STATE") == "1":
    from backend.app.services.mock_printer_state import install_mock_printer_state

    install_mock_printer_state()

router = APIRouter(prefix="/auto-print", tags=["auto-print"])


@router.post("/", status_code=202)
@router.post("", status_code=202, include_in_schema=False)
async def create_auto_print(
    file: UploadFile = File(...),
    filament_type: str = Form(...),
    color_hex: str | None = Form(default=None),
    quality: str = Form(default="Standard"),
    layer_height: float | None = Form(default=0.20),
    printer_id: int | None = Form(default=None),
    auto_orient: bool = Form(default=True),
    auto_arrange: bool = Form(default=True),
    require_approval: bool = Form(default=True),
    brim: bool = Form(default=True),
    brim_width: float = Form(default=5.0),
    current_user: User | None = Depends(require_permission_if_auth_enabled(Permission.LIBRARY_UPLOAD)),
):
    """Kick off an auto-print run. Returns immediately; poll `GET /{id}`.

    Deliberately takes individual `Form(...)` fields rather than a single
    JSON body — multipart requests can't carry a nested JSON body alongside
    a file part, and this matches the rest of the codebase's multipart
    routes (e.g. `archives.py`'s trim endpoint) rather than inventing a
    `Form(AutoPrintRequest)` model-binding shape used nowhere else here.

    Upload validation (filename charset, magic-byte sniffing, empty
    filename) is NOT duplicated here — `start_flow`'s background worker
    calls the real `upload_file()` route function, which already does all
    of that, and any rejection surfaces as `stage="failed"` with a clear
    `error` on the very next poll rather than as a synchronous 400. This
    keeps validation defined in exactly one place.
    """
    request = AutoPrintRequest(
        filament_type=filament_type,
        color_hex=color_hex,
        quality=quality,
        layer_height=layer_height,
        printer_id=printer_id,
        auto_orient=auto_orient,
        auto_arrange=auto_arrange,
        require_approval=require_approval,
        brim=brim,
        brim_width=brim_width,
    )
    flow_id = await start_flow(async_session, upload=file, request=request, current_user=current_user)
    return {"id": flow_id, "stage": "pending"}


@router.get("/options")
async def get_auto_print_options(
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_permission_if_auth_enabled(Permission.LIBRARY_UPLOAD)),
):
    """`{filaments: [...], quality_tiers: [...], defaults: {...}}` — the
    page's dropdown data, scoped to what the fleet can actually print.
    """
    filaments = await list_loaded_filaments(db)
    quality_tiers = await list_quality_tiers(db)
    defaults = await default_options(db)
    return {"filaments": filaments, "quality_tiers": quality_tiers, "defaults": defaults}


def _check_flow_visible(flow_id: int, current_user: User | None) -> None:
    """Shared per-row ownership scoping for every ``/{flow_id}...`` route
    below (`GET`, `approve`, `discard`) — pulled out of `get_auto_print_flow`
    rather than left inline once a second and third caller needed the exact
    same rule.

    Mirrors `slice_jobs.py`'s `get_slice_job`: a flow started by an API-key
    / auth-disabled caller (`owner_id=None`) is visible to any
    `LIBRARY_UPLOAD` caller (no per-row identity to scope against);
    otherwise only the flow's own starter — or a caller with
    `LIBRARY_READ_ALL` — may act on it. Raises 404 rather than 403 on the
    ownership miss so this can't be used to enumerate live flow ids.
    """
    can_read_all = current_user is None or current_user.has_permission(Permission.LIBRARY_READ_ALL.value)
    if can_read_all:
        return
    owner_id = get_flow_owner_id(flow_id)
    if current_user is None or owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Auto-print flow not found or expired")


@router.get("/{flow_id}", response_model=AutoPrintFlow)
async def get_auto_print_flow(
    flow_id: int,
    current_user: User | None = Depends(require_permission_if_auth_enabled(Permission.LIBRARY_UPLOAD)),
):
    """Poll target for one auto-print run's status."""
    flow = get_flow(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Auto-print flow not found or expired")
    _check_flow_visible(flow_id, current_user)
    return flow


@router.post("/{flow_id}/approve", response_model=AutoPrintFlow)
async def approve_auto_print_flow(
    flow_id: int,
    current_user: User | None = Depends(require_permission_if_auth_enabled(Permission.LIBRARY_UPLOAD)),
):
    """Approve a flow paused at `stage='awaiting_approval'`: queues the
    already-sliced result via the same queue-insert step the pre-approval-
    gate flow used, and lands on `stage='queued'`.

    Only valid from `awaiting_approval` — `auto_print_flow.approve_flow`
    raises `FlowStageConflictError` from any other stage (including a
    concurrent second call that lost the race to a first `approve`), which
    this route turns into a 409 with a message naming the stage actually
    found, rather than letting a caller double-queue by retrying.
    """
    flow = get_flow(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Auto-print flow not found or expired")
    _check_flow_visible(flow_id, current_user)

    try:
        updated = await approve_flow(async_session, flow_id)
    except FlowStageConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        # The flow expired (retention sweep) between the visibility check
        # above and the approve call itself — vanishingly unlikely given
        # both run back-to-back on the same event loop tick, but a 404 is
        # the honest answer if it ever does happen, not a 500.
        raise HTTPException(status_code=404, detail="Auto-print flow not found or expired")
    return updated


@router.post("/{flow_id}/discard", response_model=AutoPrintFlow)
async def discard_auto_print_flow(
    flow_id: int,
    current_user: User | None = Depends(require_permission_if_auth_enabled(Permission.LIBRARY_UPLOAD)),
):
    """Discard a flow paused at `stage='awaiting_approval'`: lands on
    `stage='discarded'` and queues nothing. The uploaded file and the
    sliced result are left alone in the library — only queueing is skipped.

    Same 404/409 shape as `approve_auto_print_flow` above, for the same
    reasons (unknown flow vs. wrong-stage-to-act-on).
    """
    flow = get_flow(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Auto-print flow not found or expired")
    _check_flow_visible(flow_id, current_user)

    try:
        updated = await discard_flow(flow_id)
    except FlowStageConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Auto-print flow not found or expired")
    return updated
