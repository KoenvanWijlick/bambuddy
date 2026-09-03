"""Tests for auto_preset_select — printer/process/filament + bed-type picking
for the auto-print flow.

Fixtures below are trimmed but shaped exactly like a real `/profiles/bundled`
response (verified against a live BambuStudio sidecar while building this
module — see docs/auto-print-pipeline-spec.md). The P1S case in particular
reproduces the empirically-confirmed fact that a P1S has *zero* process
profiles whose name contains "P1S": all of them are named `@BBL X1C` and are
only discoverable through `compatible_printers`.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.app.services import auto_preset_select as aps
from backend.app.services.slicer_api import SlicerApiService

BASE_URL = "http://test-sidecar"


@pytest.fixture(autouse=True)
def _clear_bundled_cache():
    """The bundled-profile cache is module-level (keyed by sidecar base
    URL), so it must not leak state between tests that reuse BASE_URL."""
    aps._bundled_cache.clear()
    yield
    aps._bundled_cache.clear()


# --- bundled /profiles/bundled fixture -------------------------------------

P1S_KEY = "Bambu Lab P1S 0.4 nozzle"
X1C_KEY = "Bambu Lab X1 Carbon 0.4 nozzle"
TESTBOT_KEY = "Bambu Lab TestBot"  # no nozzle-suffixed variant exists


def _process(name: str, compatible: list[str]) -> dict:
    return {"name": name, "base_id": None, "compatible_printers": compatible}


def _filament(name: str, ftype: str, compatible: list[str]) -> dict:
    return {
        "name": name,
        "base_id": None,
        "compatible_printers": compatible,
        "filament_type": ftype,
        # Gotcha 2: always null on every bundled filament profile.
        "filament_colour": None,
    }


def bundled_fixture() -> dict:
    both = [P1S_KEY, X1C_KEY]
    return {
        "printer": [
            {"name": "Bambu Lab P1S", "base_id": None},
            {"name": P1S_KEY, "base_id": P1S_KEY},
            {"name": "Bambu Lab X1 Carbon", "base_id": None},
            {"name": X1C_KEY, "base_id": None},
            {"name": TESTBOT_KEY, "base_id": None},
        ],
        "process": [
            # Note: NONE of these names contain "P1S" — matches the real
            # bundle, where a P1S's process profiles are all @BBL X1C and
            # are only discoverable through compatible_printers (Gotcha 1).
            _process("0.08mm Extra Fine @BBL X1C", both),
            _process("0.12mm Fine @BBL X1C", both),
            _process("0.12mm High Quality @BBL X1C", both),
            _process("0.16mm High Quality @BBL X1C", both),
            _process("0.16mm Optimal @BBL X1C", both),
            _process("0.20mm Standard @BBL X1C", both),
            _process("0.24mm Draft @BBL X1C", both),
            _process("0.28mm Extra Draft @BBL X1C", both),
            _process("0.20mm Standard @BBL TestBot", [TESTBOT_KEY]),
        ],
        "filament": [
            _filament("Bambu PETG Basic @BBL X1C", "PETG", both),
            _filament("Bambu PETG HF @BBL P1S 0.4 nozzle", "PETG", [P1S_KEY]),
            _filament("Generic PETG", "PETG", both),
            _filament("Bambu PLA Basic @BBL X1C", "PLA", both),
            _filament("Generic PLA", "PLA", both),
            _filament("Bambu PLA Basic @BBL TestBot", "PLA", [TESTBOT_KEY]),
        ],
    }


_MACHINE_RESOLVE = {
    P1S_KEY: {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printable_height": "250"},
    X1C_KEY: {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printable_height": 250},
    TESTBOT_KEY: {"printable_area": ["0x0", "100x0", "100x100", "0x100"], "printable_height": "100"},
}

# Measured on a live sidecar (spec Gotcha 4): neither filament is safe on
# every plate. Keys are the resolved profile's `<plate>_plate_temp` fields.
_FILAMENT_RESOLVE = {
    "Bambu PLA Basic @BBL X1C": {
        "cool_plate_temp": ["35"],
        "eng_plate_temp": ["0"],
        "hot_plate_temp": ["55"],
        "textured_plate_temp": ["55"],
        "supertack_plate_temp": ["45"],
    },
    "Bambu PLA Basic @BBL TestBot": {
        "cool_plate_temp": ["35"],
        "eng_plate_temp": ["0"],
        "hot_plate_temp": ["55"],
        "textured_plate_temp": ["55"],
        "supertack_plate_temp": ["45"],
    },
    "Bambu PETG Basic @BBL X1C": {
        "cool_plate_temp": ["0"],
        "eng_plate_temp": ["70"],
        "hot_plate_temp": ["70"],
        "textured_plate_temp": ["70"],
        "supertack_plate_temp": ["70"],
    },
    "Unprintable Filament": {
        "cool_plate_temp": ["0"],
        "eng_plate_temp": ["0"],
        "hot_plate_temp": ["0"],
        "textured_plate_temp": ["0"],
        "supertack_plate_temp": ["0"],
    },
    "Supertack Only Filament": {
        "cool_plate_temp": ["0"],
        "eng_plate_temp": ["0"],
        "hot_plate_temp": ["0"],
        "textured_plate_temp": ["0"],
        "supertack_plate_temp": ["60"],
    },
}


def _make_service(*, bundled_calls: list[int] | None = None) -> SlicerApiService:
    """A SlicerApiService whose transport answers /profiles/bundled and
    /profiles/resolve from the fixtures above, entirely in-process."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/profiles/bundled":
            if bundled_calls is not None:
                bundled_calls.append(1)
            return httpx.Response(200, json=bundled_fixture())
        if request.url.path == "/profiles/resolve":
            body = json.loads(request.content)
            category = body["category"]
            profile = body["profile"]
            name = profile["name"]
            if category == "machine":
                values = _MACHINE_RESOLVE.get(name)
            else:
                values = _FILAMENT_RESOLVE.get(name)
            if values is None:
                return httpx.Response(404, json={"message": "not found"})
            resolved = {**profile, **values}
            return httpx.Response(200, json={"profile": resolved})
        raise AssertionError(f"unexpected request: {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, timeout=10.0)
    return SlicerApiService(base_url=BASE_URL, client=client)


# --- select_presets ---------------------------------------------------------


class TestSelectPresetsP1SNameMismatch:
    """The case that breaks name-based matching."""

    @pytest.mark.asyncio
    async def test_p1s_resolves_via_compatible_printers_not_name(self):
        svc = _make_service()
        result = await aps.select_presets(
            svc,
            model_long="Bambu Lab P1S",
            nozzle_diameter=0.4,
            filament_type="PETG",
            quality="Standard",
            layer_height=0.20,
        )
        assert result.printer == P1S_KEY
        assert result.process == "0.20mm Standard @BBL X1C"
        assert "P1S" not in result.process  # the process name itself never names it
        assert result.filament == "Bambu PETG Basic @BBL X1C"
        assert result.bed_type  # bed type is always populated

    @pytest.mark.asyncio
    async def test_p1s_has_exactly_ten_process_profiles(self):
        """Sanity-checks the fixture mirrors the real bundle's shape: every
        process profile compatible with the P1S machine key, regardless of
        name, should be visible to the matcher."""
        svc = _make_service()
        options = await aps.list_quality_options(svc, machine_key=P1S_KEY)
        total = sum(len(heights) for _tier, heights in options)
        assert total == 8  # this fixture's trimmed set (real bundle: 10)


class TestSelectPresetsProcess:
    @pytest.mark.asyncio
    async def test_exact_tier_and_height(self):
        svc = _make_service()
        result = await aps.select_presets(
            svc,
            model_long="Bambu Lab X1 Carbon",
            nozzle_diameter=0.4,
            filament_type="PLA",
            quality="Draft",
            layer_height=0.24,
        )
        assert result.process == "0.24mm Draft @BBL X1C"

    @pytest.mark.asyncio
    async def test_nearest_height_within_requested_tier(self):
        svc = _make_service()
        result = await aps.select_presets(
            svc,
            model_long="Bambu Lab X1 Carbon",
            nozzle_diameter=0.4,
            filament_type="PLA",
            quality="High Quality",
            layer_height=0.10,
        )
        # High Quality offers 0.12 and 0.16; 0.10 is nearer to 0.12.
        assert result.process == "0.12mm High Quality @BBL X1C"

    @pytest.mark.asyncio
    async def test_tierless_nearest_when_tier_not_offered(self):
        svc = _make_service()
        result = await aps.select_presets(
            svc,
            model_long="Bambu Lab X1 Carbon",
            nozzle_diameter=0.4,
            filament_type="PLA",
            quality="Nonexistent Tier",
            layer_height=0.13,
        )
        assert result.process.startswith("0.12mm")

    @pytest.mark.asyncio
    async def test_no_layer_height_and_tier_missing_raises(self):
        svc = _make_service()
        with pytest.raises(aps.PresetSelectionError):
            await aps.select_presets(
                svc,
                model_long="Bambu Lab X1 Carbon",
                nozzle_diameter=0.4,
                filament_type="PLA",
                quality="Nonexistent Tier",
                layer_height=None,
            )


class TestSelectPresetsFilament:
    @pytest.mark.asyncio
    async def test_prefers_bambu_basic(self):
        svc = _make_service()
        result = await aps.select_presets(
            svc,
            model_long="Bambu Lab X1 Carbon",
            nozzle_diameter=0.4,
            filament_type="PETG",
            quality="Standard",
            layer_height=0.20,
        )
        assert result.filament == "Bambu PETG Basic @BBL X1C"

    @pytest.mark.asyncio
    async def test_no_compatible_filament_raises(self):
        svc = _make_service()
        with pytest.raises(aps.PresetSelectionError, match="ABS"):
            await aps.select_presets(
                svc,
                model_long="Bambu Lab X1 Carbon",
                nozzle_diameter=0.4,
                filament_type="ABS",
                quality="Standard",
                layer_height=0.20,
            )


class TestSelectPresetsMachine:
    @pytest.mark.asyncio
    async def test_unknown_model_raises(self):
        svc = _make_service()
        with pytest.raises(aps.PresetSelectionError):
            await aps.select_presets(
                svc,
                model_long="Bambu Lab Nonexistent",
                nozzle_diameter=0.4,
                filament_type="PLA",
                quality="Standard",
                layer_height=0.20,
            )

    @pytest.mark.asyncio
    async def test_falls_back_to_nozzleless_machine(self):
        """TestBot has no '<nozzle> nozzle'-suffixed printer entry — only a
        bare 'Bambu Lab TestBot'. select_presets must fall back to it and
        use it (not the requested nozzle key) as the compatibility filter
        for process/filament."""
        svc = _make_service()
        result = await aps.select_presets(
            svc,
            model_long="Bambu Lab TestBot",
            nozzle_diameter=0.4,
            filament_type="PLA",
            quality="Standard",
            layer_height=0.20,
        )
        assert result.printer == TESTBOT_KEY
        assert result.process == "0.20mm Standard @BBL TestBot"
        assert result.filament == "Bambu PLA Basic @BBL TestBot"


class TestBundledCache:
    @pytest.mark.asyncio
    async def test_bundled_profiles_are_cached_across_calls(self):
        calls: list[int] = []
        svc = _make_service(bundled_calls=calls)
        await aps.list_quality_options(svc, machine_key=P1S_KEY)
        await aps.list_quality_options(svc, machine_key=X1C_KEY)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_cache_refetches_after_ttl_expiry(self, monkeypatch):
        calls: list[int] = []
        svc = _make_service(bundled_calls=calls)
        clock = [1000.0]
        monkeypatch.setattr(aps.time, "monotonic", lambda: clock[0])

        await aps.list_quality_options(svc, machine_key=P1S_KEY)
        clock[0] += aps._BUNDLED_TTL_S + 1
        await aps.list_quality_options(svc, machine_key=P1S_KEY)

        assert len(calls) == 2


# --- get_build_volume -------------------------------------------------------


class TestGetBuildVolume:
    @pytest.mark.asyncio
    async def test_parses_string_height(self):
        svc = _make_service()
        volume = await aps.get_build_volume(svc, machine_key=P1S_KEY)
        assert volume == (256.0, 256.0, 250.0)

    @pytest.mark.asyncio
    async def test_parses_numeric_height(self):
        svc = _make_service()
        volume = await aps.get_build_volume(svc, machine_key=X1C_KEY)
        assert volume == (256.0, 256.0, 250.0)

    @pytest.mark.asyncio
    async def test_smaller_bed(self):
        svc = _make_service()
        volume = await aps.get_build_volume(svc, machine_key=TESTBOT_KEY)
        assert volume == (100.0, 100.0, 100.0)

    @pytest.mark.asyncio
    async def test_unresolvable_machine_raises(self):
        svc = _make_service()
        with pytest.raises(aps.PresetSelectionError):
            await aps.get_build_volume(svc, machine_key="Bambu Lab Nonexistent 0.4 nozzle")


# --- list_quality_options ----------------------------------------------------


class TestListQualityOptions:
    @pytest.mark.asyncio
    async def test_groups_by_tier_sorted(self):
        svc = _make_service()
        options = await aps.list_quality_options(svc, machine_key=P1S_KEY)
        as_dict = dict(options)
        assert as_dict["Standard"] == [0.20]
        assert as_dict["High Quality"] == [0.12, 0.16]
        assert as_dict["Extra Fine"] == [0.08]


# --- select_bed_type ---------------------------------------------------------


class TestSelectBedType:
    @pytest.mark.asyncio
    async def test_pla_prefers_textured_pei_when_no_printer_report(self):
        """PLA: eng_plate_temp is 0 (unsupported). No printer-reported plate
        given, so the preferred fallback (Textured PEI Plate, 55) wins."""
        svc = _make_service()
        bed = await aps.select_bed_type(svc, filament_preset_name="Bambu PLA Basic @BBL X1C")
        assert bed == "Textured PEI Plate"

    @pytest.mark.asyncio
    async def test_pla_ignores_reported_engineering_plate_when_unsupported(self):
        svc = _make_service()
        bed = await aps.select_bed_type(
            svc,
            filament_preset_name="Bambu PLA Basic @BBL X1C",
            printer_reported_bed_type="Engineering Plate",
        )
        # Engineering Plate is unsupported for PLA (temp 0) -> falls through
        # to the preferred fallback rather than a plate that would fail.
        assert bed == "Textured PEI Plate"

    @pytest.mark.asyncio
    async def test_petg_ignores_reported_cool_plate_when_unsupported(self):
        svc = _make_service()
        bed = await aps.select_bed_type(
            svc,
            filament_preset_name="Bambu PETG Basic @BBL X1C",
            printer_reported_bed_type="Cool Plate",
        )
        assert bed == "Textured PEI Plate"

    @pytest.mark.asyncio
    async def test_prefers_compatible_printer_reported_plate_over_textured(self):
        svc = _make_service()
        bed = await aps.select_bed_type(
            svc,
            filament_preset_name="Bambu PETG Basic @BBL X1C",
            printer_reported_bed_type="Cool Plate (SuperTack)",
        )
        assert bed == "Cool Plate (SuperTack)"

    @pytest.mark.asyncio
    async def test_supertack_plate_alias(self):
        """'Supertack Plate' and 'Cool Plate (SuperTack)' are two spellings
        of the same plate and must both key off supertack_plate_temp."""
        svc = _make_service()
        bed = await aps.select_bed_type(
            svc,
            filament_preset_name="Supertack Only Filament",
            printer_reported_bed_type="Supertack Plate",
        )
        assert bed == "Supertack Plate"

    @pytest.mark.asyncio
    async def test_smooth_pei_falls_back_to_textured_temp_key(self):
        """No bundled filament profile has been observed to expose a
        distinct smooth_plate_temp — Smooth PEI Plate must still be
        considered compatible whenever Textured PEI is (same underlying
        temp), rather than being reported as unsupported."""
        svc = _make_service()
        bed = await aps.select_bed_type(
            svc,
            filament_preset_name="Bambu PETG Basic @BBL X1C",
            printer_reported_bed_type="Smooth PEI Plate",
        )
        assert bed == "Smooth PEI Plate"

    @pytest.mark.asyncio
    async def test_all_plates_unsupported_raises_naming_filament(self):
        svc = _make_service()
        with pytest.raises(aps.PresetSelectionError, match="Unprintable Filament"):
            await aps.select_bed_type(svc, filament_preset_name="Unprintable Filament")

    @pytest.mark.asyncio
    async def test_select_presets_populates_bed_type(self):
        svc = _make_service()
        result = await aps.select_presets(
            svc,
            model_long="Bambu Lab X1 Carbon",
            nozzle_diameter=0.4,
            filament_type="PETG",
            quality="Standard",
            layer_height=0.20,
            printer_reported_bed_type="Cool Plate (SuperTack)",
        )
        assert result.bed_type == "Cool Plate (SuperTack)"
