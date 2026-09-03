"""Pick the printer/process/filament preset triplet for the auto-print flow.

The auto-print page ("Start a new print") asks the user for three things —
a file, a filament *type*, and a colour — and nothing else. Everything the
slicer actually needs (the printer profile, the process/quality profile, the
filament profile) has to be derived from that, plus the printer the pipeline
picks. This module is the derivation: given a machine (`model_long` +
`nozzle_diameter`), a `filament_type` and a quality tier, it returns the
*names* of three bundled ("standard" tier) Bambu Studio profiles that the
existing `preset_resolver.resolve_preset_ref` can turn into slice-ready JSON
via `PresetRef(source="standard", id=<name>)`.

Everything here is pure — no DB, no queue, no printer state — so it can be
unit-tested against a canned `/profiles/bundled` payload and reused by both
the auto-print worker and (later) any "what would auto-print pick" preview
in the UI.

Two empirically-verified facts (see docs/auto-print-pipeline-spec.md) shape
the matching rules below and must not be "simplified" away:

- A process profile's name carries no reliable model information. A P1S has
  *zero* process profiles with "P1S" in the name — its 10 usable profiles are
  all named ``@BBL X1C`` and are discoverable only via `compatible_printers`.
  Matching on the name instead of `compatible_printers` silently returns
  nothing for P1S / P1P / X1E and similar.
- `filament_colour` is always null on every bundled filament profile. Colour
  is not a preset-selection input at all — it drives which printer/AMS tray
  gets picked (see `auto_printer_select.py`), never which filament preset.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

from backend.app.services.slicer_api import SlicerApiError, SlicerApiService

logger = logging.getLogger(__name__)

# /profiles/bundled is ~400KB of static data for the lifetime of a given
# sidecar container (it walks a read-only `resources/profiles/BBL/` tree
# baked into the image). Re-fetching it on every auto-print request would
# mean one extra sidecar round trip per print for data that never changes
# between deploys, so it's cached in-process keyed by sidecar base URL —
# mirrors `slicer_presets._fetch_bundled_presets`'s cache, same TTL.
_BUNDLED_TTL_S = 3600.0
_bundled_cache: dict[str, tuple[float, dict]] = {}


class PresetSelectionError(Exception):
    """No preset triplet could be resolved for the requested combination.

    Message is written to be shown to the user as-is (e.g. surfaced as the
    auto-print flow's `error` field), so it names what was requested and
    what was searched, not an internal state.
    """


@dataclass(frozen=True)
class PresetSelection:
    """The bundled profile *names* chosen for a slice, plus the bed type.

    `printer` / `process` / `filament` are each used verbatim as
    `PresetRef(source="standard", id=<name>)`. `bed_type` is not a preset
    name — it's a `curr_bed_type` value (e.g. ``"Textured PEI Plate"``) that
    the caller must inject into the **process** preset stub before slicing
    (Gotcha 4): with no bed type, a slicer default of "Cool Plate" rejects
    any filament that doesn't tolerate it (PETG, notably) with "Filaments
    are not compatible with the plate type" (exit 195).
    """

    printer: str
    process: str
    filament: str
    bed_type: str


async def _get_bundled(svc: SlicerApiService) -> dict:
    """Return `/profiles/bundled`, cached in-process for `_BUNDLED_TTL_S`.

    Keyed by `svc.base_url` rather than a single global slot so a host that
    somehow talks to two different sidecars in the same process (tests, or a
    future multi-slicer setup) doesn't serve one's data for the other's key.
    """
    now = time.monotonic()
    cached = _bundled_cache.get(svc.base_url)
    if cached and now - cached[0] < _BUNDLED_TTL_S:
        return cached[1]

    raw = await svc.list_bundled_profiles()
    _bundled_cache[svc.base_url] = (now, raw)
    return raw


def _standard_stub(name: str, category: str) -> dict:
    """The same `{inherits: <name>, from: "system"}` stub `preset_resolver`
    builds for the standard tier — see its `_resolve_standard`. `/profiles/
    resolve` needs a profile *object*, never a bare name string (400 on a
    string), and this is the exact shape the sidecar's resolver expects to
    flatten against its bundled `<category>/<name>.json`.

    `category` is the sidecar's vocabulary (``"machine"`` / ``"process"`` /
    ``"filament"``), which doubles as the stub's own `type` — the CLI's
    `--load-settings` parser keys off `type` to decide how to interpret a
    settings file (see Gotcha 5 in the spec / `preset_resolver.py`'s
    `_SLOT_TO_PROFILE_TYPE`), so getting it right here matters even though
    this stub is only ever sent to `/profiles/resolve`, never to `/slice`.
    """
    return {"name": name, "inherits": name, "from": "system", "type": category}


def _machine_stub(machine_key: str) -> dict:
    return _standard_stub(machine_key, "machine")


async def _resolve_standard(svc: SlicerApiService, *, name: str, category: str) -> dict:
    """POST /profiles/resolve for a bundled profile, raising on failure.

    Shared by machine, and filament (bed-type) resolution — all need the
    flattened profile and all must fail loudly rather than silently degrade:
    unlike the SliceModal's settings panel, there is no "show blank values"
    fallback in the auto-print flow.
    """
    try:
        resolved = await svc.resolve_profile(json.dumps(_standard_stub(name, category)), category=category)
    except SlicerApiError as e:
        raise PresetSelectionError(f"Slicer sidecar unreachable while resolving {name!r}: {e}") from e
    if resolved.values is None:
        raise PresetSelectionError(
            f"Could not resolve {category} profile {name!r} ({resolved.reason}). "
            "The slicer sidecar may be outdated or unreachable."
        )
    return resolved.values


async def _resolve_machine(svc: SlicerApiService, *, machine_key: str) -> dict:
    """POST /profiles/resolve for a machine profile. See `_resolve_standard`."""
    return await _resolve_standard(svc, name=machine_key, category="machine")


def _machine_key(model_long: str, nozzle_diameter: float) -> str:
    """``"Bambu Lab <model> <d> nozzle"`` — never parse this back out of a
    process/filament *name*; only ever use it to filter `compatible_printers`
    (Gotcha 1 in the spec)."""
    model = model_long if model_long.startswith("Bambu Lab") else f"Bambu Lab {model_long}"
    return f"{model} {nozzle_diameter:.1f} nozzle"


def _nozzleless_key(model_long: str) -> str:
    model = model_long if model_long.startswith("Bambu Lab") else f"Bambu Lab {model_long}"
    return model


def _select_machine_name(bundled: dict, *, model_long: str, nozzle_diameter: float) -> str:
    machine_key = _machine_key(model_long, nozzle_diameter)
    names = {p.get("name") for p in bundled.get("printer") or []}
    if machine_key in names:
        return machine_key
    fallback = _nozzleless_key(model_long)
    if fallback in names:
        logger.info(
            "No bundled printer profile %r; falling back to nozzle-less %r",
            machine_key,
            fallback,
        )
        return fallback
    raise PresetSelectionError(
        f"No bundled Bambu Studio printer profile for {model_long!r} with a "
        f"{nozzle_diameter:.1f}mm nozzle. Checked {machine_key!r} and {fallback!r}."
    )


# A process name is `"<height>mm <tier> @<suffix>"`, e.g. "0.20mm Standard
# @BBL X1C". The tier can itself contain spaces ("High Quality", "Extra
# Fine"), so the tier group is non-greedy up to " @".
_PROCESS_NAME_RE = re.compile(r"^(?P<height>[\d.]+)mm\s+(?P<tier>.+?)\s*@")


def _parse_process_name(name: str) -> tuple[float, str] | None:
    m = _PROCESS_NAME_RE.match(name)
    if not m:
        return None
    try:
        height = float(m.group("height"))
    except ValueError:
        return None
    return height, m.group("tier")


def _select_process_name(bundled: dict, *, machine_key: str, quality: str, layer_height: float | None) -> str:
    candidates = [p for p in bundled.get("process") or [] if machine_key in (p.get("compatible_printers") or [])]
    if not candidates:
        raise PresetSelectionError(f"No bundled process (quality) profile is compatible with {machine_key!r}.")

    parsed: list[tuple[str, float, str]] = []
    for p in candidates:
        name = p.get("name") or ""
        info = _parse_process_name(name)
        if info is None:
            continue
        height, tier = info
        parsed.append((name, height, tier))
    if not parsed:
        raise PresetSelectionError(
            f"Bundled process profiles for {machine_key!r} did not match the expected "
            "'<height>mm <tier> @...' naming — cannot pick a quality preset."
        )

    if layer_height is not None:
        # 1. Exact match: this tier, this height.
        for name, height, tier in parsed:
            if tier == quality and abs(height - layer_height) < 1e-6:
                return name
        # 2. Nearest layer height within the requested tier.
        in_tier = [(name, height) for name, height, tier in parsed if tier == quality]
        if in_tier:
            return min(in_tier, key=lambda item: abs(item[1] - layer_height))[0]
    else:
        # No specific height requested: any profile in the requested tier.
        in_tier = [(name, height) for name, height, tier in parsed if tier == quality]
        if in_tier:
            # Prefer the tier's own canonical (smallest/first-listed) height.
            return min(in_tier, key=lambda item: item[1])[0]

    # 3. Tier-less nearest layer height (the requested tier doesn't exist on
    # this machine — e.g. "Extra Fine" isn't offered for every nozzle size).
    if layer_height is not None:
        logger.info(
            "Quality tier %r not offered for %r; falling back to nearest layer height across all tiers",
            quality,
            machine_key,
        )
        return min(parsed, key=lambda item: abs(item[1] - layer_height))[0]

    raise PresetSelectionError(
        f"Quality tier {quality!r} is not offered for {machine_key!r}, and no layer height was given to fall back on."
    )


def _select_filament_name(bundled: dict, *, machine_key: str, filament_type: str) -> str:
    candidates = [
        p
        for p in bundled.get("filament") or []
        if machine_key in (p.get("compatible_printers") or []) and p.get("filament_type") == filament_type
    ]
    if not candidates:
        raise PresetSelectionError(f"No bundled {filament_type!r} filament profile is compatible with {machine_key!r}.")

    names = [p.get("name") or "" for p in candidates]

    for name in names:
        if re.match(rf"^Bambu {re.escape(filament_type)} Basic\b", name):
            return name
    for name in names:
        if name.startswith(f"Bambu {filament_type}"):
            return name
    for name in names:
        if name.startswith(f"Generic {filament_type}"):
            return name
    return names[0]


async def select_presets(
    svc: SlicerApiService,
    *,
    model_long: str,
    nozzle_diameter: float,
    filament_type: str,
    quality: str,
    layer_height: float | None,
    printer_reported_bed_type: str | None = None,
) -> PresetSelection:
    """Pick the printer/process/filament bundled profile names for a slice.

    Also picks a compatible bed type (Gotcha 4) for the chosen filament —
    `printer_reported_bed_type` is the plate the printer says is installed
    (read from live MQTT telemetry by the caller; this module stays DB/
    printer-manager free), preferred when it can actually take the filament.

    Raises `PresetSelectionError` — with a message safe to show the user —
    when no combination satisfies the request. Never raises for a sidecar
    connectivity problem without wrapping it in `PresetSelectionError` first;
    callers only need to catch the one exception type.
    """
    try:
        bundled = await _get_bundled(svc)
    except SlicerApiError as e:
        raise PresetSelectionError(f"Could not reach the slicer sidecar to list bundled profiles: {e}") from e

    printer_name = _select_machine_name(bundled, model_long=model_long, nozzle_diameter=nozzle_diameter)
    # Use the *matched* printer name (which may be the nozzle-less fallback)
    # as the compatibility key for process/filament lookups, so a fallback on
    # step 1 doesn't leave steps 2/3 filtering against a key nothing lists.
    process_name = _select_process_name(bundled, machine_key=printer_name, quality=quality, layer_height=layer_height)
    filament_name = _select_filament_name(bundled, machine_key=printer_name, filament_type=filament_type)
    bed_type = await select_bed_type(
        svc, filament_preset_name=filament_name, printer_reported_bed_type=printer_reported_bed_type
    )

    return PresetSelection(printer=printer_name, process=process_name, filament=filament_name, bed_type=bed_type)


# `curr_bed_type` value -> the resolved filament profile's temp key for that
# plate. The 7 keys are BambuStudio/OrcaSlicer's full canonical `bed_type`
# vocabulary (see `SliceRequest.bed_type`'s docstring in schemas/slicer.py,
# #1337) — not just the 5 the spec's own measurements covered. Order is the
# fallback search order for step 3 of `select_bed_type` ("the first plate
# with a non-zero temp"), and matches that docstring's ordering — deliberately
# NOT reordered to put Textured PEI first, because that preference is already
# handled as its own explicit step and giving it double weight here would
# just be confusing to read.
#
# 'Cool Plate (SuperTack)' and 'Supertack Plate' are the same physical plate
# under two spellings both slicers have shipped; both key off the one
# `supertack_plate_temp` field a resolved profile actually carries.
_BED_TYPE_TEMP_KEYS: dict[str, str] = {
    "Cool Plate": "cool_plate_temp",
    "Engineering Plate": "eng_plate_temp",
    "High Temp Plate": "hot_plate_temp",
    "Textured PEI Plate": "textured_plate_temp",
    "Smooth PEI Plate": "smooth_plate_temp",
    "Cool Plate (SuperTack)": "supertack_plate_temp",
    "Supertack Plate": "supertack_plate_temp",
}

# 'Smooth PEI Plate' -> `smooth_plate_temp` has never been observed on a
# live sidecar resolve (checked against bundled PLA/PETG Basic profiles) —
# every profile seen exposes cool/eng/hot/textured/supertack only. Rather
# than invent a key that may not exist, treat Smooth PEI as the finish-only
# variant of Textured PEI it physically is and borrow that plate's temp
# when the primary key is absent. `_plate_temp` still prefers a genuine
# `smooth_plate_temp` the moment some sidecar/profile actually reports one.
_BED_TYPE_FALLBACK_TEMP_KEYS: dict[str, str] = {
    "Smooth PEI Plate": "textured_plate_temp",
}

_PREFERRED_FALLBACK_BED_TYPE = "Textured PEI Plate"


def _plate_temp(resolved: dict, bed_type: str) -> float | None:
    """The build-plate temp `resolved` (a resolved filament profile) reports
    for `bed_type`, preferring the plate's own key and falling back to
    `_BED_TYPE_FALLBACK_TEMP_KEYS` only when the primary key is genuinely
    absent (not merely zero — zero is a real "unsupported" answer, not a
    reason to fall back)."""
    primary_key = _BED_TYPE_TEMP_KEYS[bed_type]
    temp = _first_scalar(resolved.get(primary_key))
    if temp is not None:
        return temp
    fallback_key = _BED_TYPE_FALLBACK_TEMP_KEYS.get(bed_type)
    if fallback_key is not None:
        return _first_scalar(resolved.get(fallback_key))
    return None


async def select_bed_type(
    svc: SlicerApiService,
    *,
    filament_preset_name: str,
    printer_reported_bed_type: str | None = None,
) -> str:
    """Pick a `curr_bed_type` the chosen filament actually tolerates.

    Data-driven, deliberately: there is no hard-coded filament->plate table
    here (mirroring the "no hand-maintained build-volume table" rule) because
    there is no plate that is universally safe. Measured on a live sidecar,
    PLA's `eng_plate_temp` and PETG's `cool_plate_temp` are both `0` — i.e.
    unsupported — so a fixed default plate is wrong for *some* common
    filament sooner or later. Instead this resolves the filament profile via
    `/profiles/resolve` and reads its `<plate>_plate_temp` keys straight off
    the response; a `0` means the slicer itself considers that plate
    unsupported for this filament (Gotcha 4).

    Preference order:
    1. `printer_reported_bed_type`, when it is a known plate and compatible —
       don't ask the user to swap a plate that's already correct.
    2. `"Textured PEI Plate"` — the most common plate shipped across the
       current Bambu Lab lineup.
    3. The first remaining plate (table order) with a non-zero temp.

    Raises `PresetSelectionError` naming the filament when every plate is
    unsupported.
    """
    resolved = await _resolve_standard(svc, name=filament_preset_name, category="filament")

    supported: dict[str, float] = {}
    for bed_type in _BED_TYPE_TEMP_KEYS:
        temp = _plate_temp(resolved, bed_type)
        if temp is not None and temp > 0:
            supported[bed_type] = temp

    if printer_reported_bed_type and printer_reported_bed_type in supported:
        return printer_reported_bed_type
    if _PREFERRED_FALLBACK_BED_TYPE in supported:
        return _PREFERRED_FALLBACK_BED_TYPE
    if supported:
        return next(iter(supported))

    raise PresetSelectionError(
        f"No print bed supports {filament_preset_name!r}: every known plate type "
        f"({', '.join(_BED_TYPE_TEMP_KEYS)}) reports a 0 build-plate temperature for it."
    )


def _parse_area_points(printable_area: object) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    if not isinstance(printable_area, list):
        return points
    for raw in printable_area:
        if not isinstance(raw, str) or "x" not in raw:
            continue
        x_s, _, y_s = raw.partition("x")
        try:
            points.append((float(x_s), float(y_s)))
        except ValueError:
            continue
    return points


def _first_scalar(value: object) -> float | None:
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


async def get_build_volume(svc: SlicerApiService, *, machine_key: str) -> tuple[float, float, float]:
    """(x, y, z) mm bed size for `machine_key`, from `/profiles/resolve`.

    Deliberately asks the sidecar rather than maintaining a hand-written
    per-model table — there is none in the codebase today, and this is why:
    a table drifts the moment Bambu Studio's bundled profiles change, while
    the resolved profile is always exactly what the sidecar would slice
    against.
    """
    resolved = await _resolve_machine(svc, machine_key=machine_key)
    points = _parse_area_points(resolved.get("printable_area"))
    if not points:
        raise PresetSelectionError(f"Resolved machine profile for {machine_key!r} has no usable printable_area.")
    x = max(p[0] for p in points)
    y = max(p[1] for p in points)
    z = _first_scalar(resolved.get("printable_height"))
    if z is None:
        raise PresetSelectionError(f"Resolved machine profile for {machine_key!r} has no usable printable_height.")
    return (x, y, z)


async def list_quality_options(svc: SlicerApiService, *, machine_key: str) -> list[tuple[str, list[float]]]:
    """`[(tier, sorted layer heights)]` offered for `machine_key`.

    Powers the auto-print page's Quality / Layer Height dropdowns so a user
    can only pick a combination that actually resolves to a real profile.
    """
    bundled = await _get_bundled(svc)
    candidates = [p for p in bundled.get("process") or [] if machine_key in (p.get("compatible_printers") or [])]

    tiers: dict[str, set[float]] = {}
    for p in candidates:
        info = _parse_process_name(p.get("name") or "")
        if info is None:
            continue
        height, tier = info
        tiers.setdefault(tier, set()).add(height)

    return [(tier, sorted(heights)) for tier, heights in sorted(tiers.items())]
