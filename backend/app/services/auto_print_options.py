"""Feeds the "Start a new print" page's dropdowns.

The whole point of the auto-print page is that the user can only pick
something the fleet can actually print: a filament *type* + *colour*
combination that is genuinely loaded somewhere right now, and a quality
tier that resolves to a real bundled process profile. This module answers
"what's loaded" (from live AMS/vt_tray telemetry, via `printer_manager`)
and "what quality tiers exist" (from the sidecar's bundled profiles, via
`auto_preset_select.list_quality_options`) so the frontend never has to
guess and the backend never has to reject a combination it advertised.

Deliberately DB-and-printer-manager-only, no slicer-side selection logic —
that's `auto_preset_select.py` / `auto_printer_select.py`. This module is
read-only and side-effect free, which is what lets `GET /auto-print/options`
be polled cheaply and repeatedly by the page on every mount.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

# Reference machine used to ask the sidecar which quality tiers / layer
# heights exist at all, for the page's Quality + Layer Height dropdowns.
# This is deliberately NOT the printer that ends up doing the print — that
# choice happens later, per-request, in auto_printer_select.select_printer.
# X1C's 0.4mm nozzle is picked because it's Bambu's flagship / most broadly
# mirrored profile set (every quality tier in the spec's table was measured
# against it) — a P1S-only fleet would still get a reasonable dropdown even
# though the final printer might differ, and `auto_preset_select.select_presets`
# re-resolves the *actual* tier/height against the *actual* chosen machine
# at slice time regardless of what this list offered.
_REFERENCE_MACHINE_KEY = "Bambu Lab X1 Carbon 0.4 nozzle"

# Static per docs/auto-print-pipeline-spec.md: "Defaults for the page:
# quality Standard, layer height 0.20". Not derived from the sidecar because
# the default has to be stable and instant even when the sidecar is briefly
# unreachable — the page still needs *some* preselected value to show.
_DEFAULT_QUALITY = "Standard"
_DEFAULT_LAYER_HEIGHT = 0.20


class LoadedFilament(BaseModel):
    """One (type, colour) combination actually loaded somewhere in the fleet
    right now, and where."""

    filament_type: str
    color_hex: str
    color_name: str | None = None
    printer_ids: list[int]
    tray_count: int


def _normalise_color_hex(raw: str | None) -> str | None:
    """AMS telemetry reports colour as 8-hex ``RRGGBBAA`` (Bambu always
    includes an alpha byte); the rest of the app — and the request schema's
    ``color_hex`` — deals in 6-hex ``#RRGGBB``. Drops the alpha byte and
    normalises case so two trays of the same visual colour compare equal.
    """
    if not raw:
        return None
    value = raw.lstrip("#").strip()
    if len(value) < 6:
        return None
    return f"#{value[:6].upper()}"


def _tray_reading(printer_id: int) -> dict:
    """The best AMS/vt_tray reading available for a printer right now.

    Mirrors `PrintScheduler._tray_reading`: live status first (a printer
    keeps its last-known `raw_data` after going offline — `mark_power_off`
    blanks `connected`/`state` but leaves trays alone), falling back to
    `printer_manager.last_known_trays` for a printer whose MQTT client has
    been dropped entirely and taken its status with it. An empty result
    means "we have never heard from this printer", not "nothing is loaded".
    """
    status = printer_manager.get_status(printer_id)
    raw = (status.raw_data if status else None) or {}
    if raw.get("ams") or raw.get("vt_tray"):
        return raw
    return printer_manager.last_known_trays(printer_id)


async def list_loaded_filaments(db: AsyncSession) -> list[LoadedFilament]:
    """What filament type/colour combinations are actually loaded, fleet-wide.

    De-duplicates on ``(filament_type, color_hex)`` — a page dropdown entry
    per genuinely distinct choice, not one per tray. ``printer_ids`` names
    every active printer offering that combination; ``tray_count`` is the
    total number of physical trays (across every printer) loaded with it,
    so the UI can hint at how much of it is on hand.
    """
    result = await db.execute(select(Printer).where(Printer.is_active == True))  # noqa: E712
    printers = result.scalars().all()

    combos: dict[tuple[str, str], LoadedFilament] = {}
    for printer in printers:
        raw = _tray_reading(printer.id)
        trays: list[dict] = []
        for ams_unit in raw.get("ams") or []:
            trays.extend(ams_unit.get("tray") or [])
        trays.extend(raw.get("vt_tray") or [])

        for tray in trays:
            filament_type = (tray.get("tray_type") or "").strip()
            color_hex = _normalise_color_hex(tray.get("tray_color"))
            if not filament_type or not color_hex:
                # An empty/unloaded slot reports no tray_type — not a
                # printable option, so it's silently skipped rather than
                # surfaced as a mystery blank entry in the dropdown.
                continue
            key = (filament_type.upper(), color_hex)
            existing = combos.get(key)
            if existing is None:
                combos[key] = LoadedFilament(
                    filament_type=filament_type.upper(),
                    color_hex=color_hex,
                    color_name=tray.get("tray_id_name") or None,
                    printer_ids=[printer.id],
                    tray_count=1,
                )
            else:
                existing.tray_count += 1
                if printer.id not in existing.printer_ids:
                    existing.printer_ids.append(printer.id)
                if existing.color_name is None and tray.get("tray_id_name"):
                    existing.color_name = tray.get("tray_id_name")

    return sorted(combos.values(), key=lambda f: (f.filament_type, f.color_hex))


async def list_quality_tiers(db: AsyncSession) -> list[dict]:
    """``[{tier, layer_heights: [...]}]`` for the page's Quality dropdown.

    Asked of the reference machine (see `_REFERENCE_MACHINE_KEY`) rather
    than hard-coded, per the spec's rule that there is deliberately no
    hand-maintained profile table in this codebase — the sidecar's bundled
    profiles are the only source of truth and can change between images.
    Degrades to an empty list (not an error) when the sidecar can't be
    reached, so the page still renders; the Print button then fails loudly
    at submit time instead, via the real slicing-stage error.
    """
    from backend.app.api.routes.slicer_presets import _resolve_slicer_api_url
    from backend.app.services.auto_preset_select import PresetSelectionError, list_quality_options
    from backend.app.services.slicer_api import SlicerApiError, SlicerApiService

    api_url = await _resolve_slicer_api_url(db)
    if not api_url:
        logger.info("No slicer sidecar configured; auto-print quality tiers unavailable")
        return []

    try:
        async with SlicerApiService(base_url=api_url) as svc:
            tiers = await list_quality_options(svc, machine_key=_REFERENCE_MACHINE_KEY)
    except (SlicerApiError, PresetSelectionError) as e:
        logger.info("Could not list quality tiers from sidecar at %s: %s", api_url, e)
        return []

    return [{"tier": tier, "layer_heights": heights} for tier, heights in tiers]


async def default_options(db: AsyncSession) -> dict:
    """Preselected values for the Quality / Layer Height controls."""
    return {"quality": _DEFAULT_QUALITY, "layer_height": _DEFAULT_LAYER_HEIGHT}
