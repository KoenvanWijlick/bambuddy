"""Pick the printer (and, when possible, the AMS tray) for the auto-print flow.

The auto-print page only asks the user for a filament *type* and *colour* —
never a printer. This module is what turns that into a concrete `Printer`
row: it looks at every active printer's live (or last-known) AMS state, its
build volume, and its current business, and ranks the candidates the way a
person would if they walked up to the farm and asked "which of these can
take this job right now".

Deliberately DB + `printer_manager` aware (unlike `auto_preset_select`,
which is pure): printer state is inherently live, mutable, per-installation
data, so there is nothing to gain from pushing that dependency up to a
caller here.

Colour is never a preset-selection input (see `auto_preset_select`'s module
docstring, Gotcha 2) — it only ever narrows *which tray on which printer*,
which is exactly what this module does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.auto_preset_select import PresetSelectionError, get_build_volume
from backend.app.services.printer_manager import printer_manager
from backend.app.services.slicer_api import SlicerApiError, SlicerApiService
from backend.app.services.slot_nozzle import DEFAULT_NOZZLE_DIAMETER, nozzle_diameter_for_extruder
from backend.app.utils.color_utils import colors_similar
from backend.app.utils.filament_types import canonical_filament_type

logger = logging.getLogger(__name__)

# Same tolerance the rest of the codebase uses for "close enough" colour
# matching (`print_scheduler`'s RFID/firmware slop, `colors_similar`'s own
# default is 50 but the spec calls for ~40 here) — a spool read back a little
# warm or cool by the AMS's sensor must still count as the colour the user
# asked for.
_COLOR_MATCH_THRESHOLD = 40

# `Printer.model` stores Bambuddy's short code ("X1C", "P1S", "A1 Mini", ...)
# — see `backend.app.utils.printer_models.PRINTER_MODEL_MAP`, whose *values*
# are exactly this set of codes. The sidecar's bundled `/profiles/bundled`
# listing, though, addresses printers by their long name ("Bambu Lab X1
# Carbon"), and `compatible_printers` entries are that long name plus a
# nozzle suffix. This is the inverse of `PRINTER_MODEL_MAP` — but not a
# naive `{v: k for k, v in PRINTER_MODEL_MAP.items()}`, because several long
# spellings collapse onto one short code (three different keys all map to
# "A1 Mini") and only one of them is the sidecar's actual bundled name.
# Verified against a live BambuStudio sidecar's `/profiles/bundled` (notably
# "Bambu Lab A1 mini" — lowercase "mini", not the capitalised form Bambuddy
# normalizes *to*). This is a name-spelling correspondence, not the
# hand-maintained build-volume table the spec forbids — there is no
# dimension or capability data here, only which string the sidecar answers
# to.
_MODEL_SHORT_TO_LONG: dict[str, str] = {
    "X1C": "Bambu Lab X1 Carbon",
    "X1": "Bambu Lab X1",
    "X1E": "Bambu Lab X1E",
    "P1S": "Bambu Lab P1S",
    "P1P": "Bambu Lab P1P",
    "P2S": "Bambu Lab P2S",
    "A1": "Bambu Lab A1",
    "A1 Mini": "Bambu Lab A1 mini",
    "H2D": "Bambu Lab H2D",
    "H2D Pro": "Bambu Lab H2D Pro",
    "H2C": "Bambu Lab H2C",
    "H2S": "Bambu Lab H2S",
    "X2D": "Bambu Lab X2D",
    "A2L": "Bambu Lab A2L",
}


def model_long_name(model_short: str | None) -> str | None:
    """`Printer.model` short code -> the sidecar's long printer-profile name.

    Returns `None` for a model Bambuddy doesn't recognise, so callers can
    decide whether to skip a build-volume check rather than guess a name the
    sidecar will just fail to find.
    """
    if not model_short:
        return None
    return _MODEL_SHORT_TO_LONG.get(model_short)


class NoPrinterAvailableError(Exception):
    """No printer could be selected. The message is shown directly in the
    auto-print UI, so it always says *why*: no idle printer, the requested
    colour isn't loaded anywhere, or the model doesn't fit any configured
    bed. Never a bare "no printer available"."""


@dataclass(frozen=True)
class TrayMatch:
    """One AMS (or external-spool) slot that satisfies the request."""

    printer_id: int
    ams_id: int
    tray_id: int
    global_tray_id: int  # the value PrintQueueItemCreate.ams_mapping expects
    filament_type: str
    color_hex: str


@dataclass(frozen=True)
class PrinterSelection:
    printer: Printer
    nozzle_diameter: float
    tray: TrayMatch | None
    reason: str


def _global_tray_id(ams_id: int, tray_id: int) -> int:
    """Bambu's flat tray addressing. Mirrors `print_scheduler._global_tray_id`
    exactly — the two must agree, since this id is what ends up in
    `PrintQueueItem.ams_mapping` and the scheduler is what reads it back
    against live tray telemetry."""
    return ams_id if ams_id >= 128 else ams_id * 4 + tray_id


def _normalize_color_hex(color: str | None) -> str:
    """`#RRGGBB`, alpha and casing stripped. Same shape `print_scheduler.
    _normalize_color` stores on its loaded-filament dicts, so a colour
    threaded through this module compares byte-identically with one that
    went through the scheduler's."""
    if not color:
        return "#808080"
    return f"#{color.replace('#', '')[:6].upper()}"


def _printer_trays(printer_id: int) -> list[dict]:
    """Every tray this printer is currently (or was last) reporting.

    Live telemetry first; `printer_manager.last_known_trays` when the
    printer has no active MQTT client (offline, or never reconnected since
    Bambuddy started) so an offline printer with a match can still be
    picked — the queue item just waits for the printer like any other
    pending item; see `printer_manager.last_known_trays`'s docstring for why
    this is the right fallback rather than treating "no client" as "nothing
    loaded".
    """
    status = printer_manager.get_status(printer_id)
    raw = (status.raw_data if status else None) or {}
    if not raw.get("ams") and not raw.get("vt_tray"):
        raw = printer_manager.last_known_trays(printer_id)

    trays: list[dict] = []
    for ams_unit in raw.get("ams") or []:
        ams_id = int(ams_unit.get("id", 0))
        for tray in ams_unit.get("tray", []) or []:
            tray_type = tray.get("tray_type")
            if not tray_type:
                continue
            tray_id = int(tray.get("id", 0))
            trays.append(
                {
                    "ams_id": ams_id,
                    "tray_id": tray_id,
                    "global_tray_id": _global_tray_id(ams_id, tray_id),
                    "filament_type": tray_type,
                    "color_hex": _normalize_color_hex(tray.get("tray_color")),
                }
            )
    for vt in raw.get("vt_tray") or []:
        tray_type = vt.get("tray_type")
        if not tray_type:
            continue
        tray_id = int(vt.get("id", 254))
        trays.append(
            {
                "ams_id": -1,
                "tray_id": tray_id,
                "global_tray_id": tray_id,
                "filament_type": tray_type,
                "color_hex": _normalize_color_hex(vt.get("tray_color")),
            }
        )
    return trays


def _best_tray(printer_id: int, *, filament_type: str, color_hex: str | None) -> TrayMatch | None:
    """The best-matching loaded tray on this printer, or `None` if nothing
    loaded satisfies `filament_type` (+ `color_hex`, when given)."""
    wanted_type = canonical_filament_type(filament_type)
    wanted_color = _normalize_color_hex(color_hex) if color_hex else None

    matches = [t for t in _printer_trays(printer_id) if canonical_filament_type(t["filament_type"]) == wanted_type]
    if wanted_color is not None:
        # colors_similar wants bare RRGGBB(AA) — no leading '#' (see its
        # docstring); _normalize_color_hex always produces one, so strip it
        # back off here rather than at every call site.
        matches = [
            t
            for t in matches
            if colors_similar(wanted_color.lstrip("#"), t["color_hex"].lstrip("#"), _COLOR_MATCH_THRESHOLD)
        ]
    if not matches:
        return None

    # Multiple slots can satisfy the request (several spools of the same
    # colour+type, or a colourless request matching every PETG slot) — pick
    # the closest colour match first, tray/ams id as a stable tie-break so
    # the choice doesn't jitter between otherwise-equal ticks.
    def sort_key(t: dict) -> tuple[float, int, int]:
        distance = 0.0
        if wanted_color is not None:
            distance = _rgb_distance(wanted_color, t["color_hex"])
        return (distance, t["ams_id"], t["tray_id"])

    best = min(matches, key=sort_key)
    return TrayMatch(
        printer_id=printer_id,
        ams_id=best["ams_id"],
        tray_id=best["tray_id"],
        global_tray_id=best["global_tray_id"],
        filament_type=best["filament_type"],
        color_hex=best["color_hex"],
    )


def _rgb_distance(hex_a: str, hex_b: str) -> float:
    a, b = hex_a.lstrip("#"), hex_b.lstrip("#")
    if len(a) < 6 or len(b) < 6:
        return 0.0
    try:
        ra, ga, ba = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
        rb, gb, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
    except ValueError:
        return 0.0
    return ((ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2) ** 0.5


# Status rank: lower is better. Mirrors the busy/offline distinction
# `print_scheduler` already makes via `printer_manager.is_connected` /
# `is_print_active`, so "idle" here means the same thing it means to the
# dispatcher that will actually start this job.
_RANK_IDLE = 0
_RANK_BUSY = 1
_RANK_OFFLINE = 2


def _status_rank(printer_id: int) -> int:
    if not printer_manager.is_connected(printer_id):
        return _RANK_OFFLINE
    if printer_manager.is_print_active(printer_id):
        return _RANK_BUSY
    return _RANK_IDLE


async def _printer_nozzle_diameter(printer: Printer) -> float:
    state = printer_manager.get_status(printer.id)
    raw = nozzle_diameter_for_extruder(state, None, printer.model)
    try:
        return float(raw or DEFAULT_NOZZLE_DIAMETER)
    except ValueError:
        return float(DEFAULT_NOZZLE_DIAMETER)


async def _fits_build_volume(
    svc: SlicerApiService,
    printer: Printer,
    nozzle_diameter: float,
    model_size_mm: tuple[float, float, float],
    volume_cache: dict[str, tuple[float, float, float] | None],
) -> bool:
    """True when `model_size_mm` fits `printer`'s bed, fail-open on doubt.

    "Doubt" covers an unrecognised `Printer.model` and a sidecar/profile
    error — in both cases the check simply isn't performed for this printer
    rather than excluding it, matching how the rest of the auto-pick logic
    treats an auxiliary signal it can't compute (see `print_scheduler`'s
    keep-warm pass: never let a secondary check wedge the primary decision).
    A genuinely oversized model is still caught by the printers whose
    profile *does* resolve; if none resolve, the size check is a no-op and
    other rejection reasons (filament/colour) still apply.
    """
    long_name = model_long_name(printer.model)
    if long_name is None:
        logger.info("Skipping build-volume check for printer %s: unrecognised model %r", printer.id, printer.model)
        return True

    machine_key = f"{long_name} {nozzle_diameter:.1f} nozzle"
    if machine_key not in volume_cache:
        try:
            volume_cache[machine_key] = await get_build_volume(svc, machine_key=machine_key)
        except (PresetSelectionError, SlicerApiError) as e:
            logger.info("Skipping build-volume check for %r: %s", machine_key, e)
            volume_cache[machine_key] = None

    volume = volume_cache[machine_key]
    if volume is None:
        return True

    bed_x, bed_y, bed_z = volume
    mx, my, mz = model_size_mm
    # Bed X/Y is a square-ish plate the model can be rotated 90 degrees
    # within (the slicer's own auto-arrange does exactly this), so either
    # axis assignment counting as a fit avoids rejecting a print that would
    # only fail if forced into one specific orientation.
    fits_xy = (mx <= bed_x and my <= bed_y) or (mx <= bed_y and my <= bed_x)
    return fits_xy and mz <= bed_z


async def _resolve_slicer_service(db: AsyncSession) -> SlicerApiService | None:
    """Build a `SlicerApiService` for whichever sidecar `preferred_slicer`
    points at, mirroring the resolution `slicer_presets._resolve_slicer_api_url`
    already implements (itself mirroring `library.py`'s slice route). Reused
    directly rather than re-deriving the same setting-lookup a fourth time.

    Returns `None` when no sidecar is configured, so the build-volume check
    degrades to "not performed" instead of failing every auto-print request
    outright — a missing/misconfigured sidecar is `select_presets`'s problem
    to raise loudly on, later in the flow; this module's job is only to
    *rank* printers, and volume just becomes an unavailable signal.
    """
    from backend.app.api.routes.slicer_presets import _resolve_slicer_api_url

    try:
        api_url = await _resolve_slicer_api_url(db)
        if not api_url:
            return None
        return SlicerApiService(base_url=api_url)
    except Exception as e:  # noqa: BLE001 — never let sidecar setup break printer ranking
        logger.warning("Could not set up slicer sidecar client for build-volume checks: %s", e)
        return None


async def _pending_queue_depth(db: AsyncSession, printer_id: int) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(PrintQueueItem)
        .where(PrintQueueItem.printer_id == printer_id)
        .where(PrintQueueItem.status == "pending")
    )
    return int(result.scalar_one())


def _describe_tray(tray: TrayMatch) -> str:
    return (
        f"{tray.filament_type} loaded in AMS {tray.ams_id} slot {tray.tray_id}"
        if tray.ams_id >= 0
        else (f"{tray.filament_type} loaded on the external spool")
    )


async def select_printer(
    db: AsyncSession,
    *,
    filament_type: str,
    color_hex: str | None,
    model_size_mm: tuple[float, float, float] | None,
    explicit_printer_id: int | None = None,
) -> PrinterSelection:
    """Pick the printer (and AMS tray) the auto-print flow should use.

    Raises `NoPrinterAvailableError` with a user-facing reason when nothing
    qualifies. Ranking, highest first, among printers whose bed fits the
    model and that have a loaded tray of the right type (+colour):
    idle+connected, then busy, then offline; ties broken by the shortest
    pending queue.

    `explicit_printer_id` bypasses ranking (the user picked this printer on
    purpose) but the bed-size check still applies — a print that categorically
    cannot fit the plate is refused rather than queued to fail at slice time.
    A tray match is attempted but not required for an explicit pick: the
    user may intend to load the spool before the job reaches the front of
    the queue, so `tray=None` is a valid, non-error result here.
    """
    svc = await _resolve_slicer_service(db)
    volume_cache: dict[str, tuple[float, float, float] | None] = {}

    try:
        if explicit_printer_id is not None:
            printer = await db.get(Printer, explicit_printer_id)
            if printer is None or not printer.is_active:
                raise NoPrinterAvailableError(f"Printer {explicit_printer_id} is not available.")
            nozzle = await _printer_nozzle_diameter(printer)
            if model_size_mm is not None and svc is not None:
                if not await _fits_build_volume(svc, printer, nozzle, model_size_mm, volume_cache):
                    mx, my, mz = model_size_mm
                    raise NoPrinterAvailableError(
                        f"The model ({mx:.0f}x{my:.0f}x{mz:.0f} mm) is larger than {printer.name}'s print bed."
                    )
            tray = _best_tray(printer.id, filament_type=filament_type, color_hex=color_hex)
            reason = (
                f"Manually selected — {_describe_tray(tray)}"
                if tray
                else f"Manually selected — no {filament_type} currently loaded"
            )
            return PrinterSelection(printer=printer, nozzle_diameter=nozzle, tray=tray, reason=reason)

        result = await db.execute(select(Printer).where(Printer.is_active == True).order_by(Printer.id))  # noqa: E712
        printers = list(result.scalars().all())
        if not printers:
            raise NoPrinterAvailableError("No active printers are configured.")

        oversized = 0
        no_type = 0
        no_color = 0
        candidates: list[tuple[Printer, float, TrayMatch]] = []

        for printer in printers:
            nozzle = await _printer_nozzle_diameter(printer)
            if model_size_mm is not None and svc is not None:
                if not await _fits_build_volume(svc, printer, nozzle, model_size_mm, volume_cache):
                    oversized += 1
                    continue

            trays = _printer_trays(printer.id)
            wanted_type = canonical_filament_type(filament_type)
            has_type = any(canonical_filament_type(t["filament_type"]) == wanted_type for t in trays)
            tray = _best_tray(printer.id, filament_type=filament_type, color_hex=color_hex)
            if tray is None:
                if has_type:
                    no_color += 1
                else:
                    no_type += 1
                continue

            candidates.append((printer, nozzle, tray))

        if not candidates:
            mx = my = mz = None
            if model_size_mm is not None:
                mx, my, mz = model_size_mm
            raise NoPrinterAvailableError(
                _no_printer_reason(len(printers), oversized, no_type, no_color, filament_type, color_hex, mx, my, mz)
            )

        ranked = []
        for printer, nozzle, tray in candidates:
            rank = _status_rank(printer.id)
            depth = await _pending_queue_depth(db, printer.id)
            ranked.append((rank, depth, printer.id, printer, nozzle, tray))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]))

        rank, depth, _pid, printer, nozzle, tray = ranked[0]
        reason = _selection_reason(rank, depth, tray)
        return PrinterSelection(printer=printer, nozzle_diameter=nozzle, tray=tray, reason=reason)
    finally:
        if svc is not None:
            await svc.close()


def _selection_reason(rank: int, depth: int, tray: TrayMatch) -> str:
    described = _describe_tray(tray)
    if rank == _RANK_IDLE:
        return f"Idle, {described}"
    if rank == _RANK_BUSY:
        return f"Shortest queue ({depth} pending), {described}"
    return f"Offline, will start once powered on — {described}"


def _no_printer_reason(
    total: int,
    oversized: int,
    no_type: int,
    no_color: int,
    filament_type: str,
    color_hex: str | None,
    mx: float | None,
    my: float | None,
    mz: float | None,
) -> str:
    """Build the `NoPrinterAvailableError` message. Named reasons only —
    never a bare "no printer available" (see the class docstring)."""
    if oversized == total:
        return f"The model ({mx:.0f}x{my:.0f}x{mz:.0f} mm) is larger than the print bed on every configured printer."
    if no_type + oversized == total and no_type > 0:
        return f"No printer currently has {filament_type} loaded."
    if no_color > 0 and color_hex:
        return f"No printer has {filament_type} loaded in the requested colour ({color_hex})."
    if no_type > 0:
        return f"No printer currently has {filament_type} loaded."
    return f"No printer is currently available for {filament_type}."
