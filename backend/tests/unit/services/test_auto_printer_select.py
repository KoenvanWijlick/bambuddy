"""Tests for auto_printer_select — the printer/AMS-tray picker for the
auto-print flow.

Uses the shared `printer_factory` / `db_session` fixtures from conftest.py
rather than inventing new printer fakes. Live printer state (AMS trays,
connectivity, print-active) is mocked at `auto_printer_select.printer_manager`
directly — the module-local binding this file imports into, mirroring how
`printer_manager` is consumed elsewhere in the codebase (e.g. print_scheduler).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import auto_printer_select as aps
from backend.app.services.auto_preset_select import PresetSelectionError


class _FakeSlicerService:
    """Stand-in for SlicerApiService — never makes a real HTTP call in these
    tests. `select_printer` always resolves a service (even when it turns
    out not to need one), so this keeps that resolution hermetic; tests
    that care about the build-volume path patch `get_build_volume` directly.
    """

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _patch_slicer_service():
    with patch(
        "backend.app.services.auto_printer_select._resolve_slicer_service",
        AsyncMock(return_value=_FakeSlicerService()),
    ):
        yield


def _status(raw_data: dict) -> MagicMock:
    # `nozzles=[]` makes `nozzle_diameter_for_extruder` fall back to the
    # default cleanly instead of chasing MagicMock auto-attributes.
    return MagicMock(raw_data=raw_data, nozzles=[])


def _ams_tray(ams_id: int, tray_id: int, ftype: str, color: str) -> dict:
    return {"id": tray_id, "tray_type": ftype, "tray_color": color}


def _raw_data(*trays_by_ams: tuple[int, list[dict]]) -> dict:
    return {"ams": [{"id": ams_id, "tray": trays} for ams_id, trays in trays_by_ams]}


@pytest.fixture
def pm():
    """A configurable `printer_manager` double. `states[printer_id]` controls
    what `get_status` / `is_connected` / `is_print_active` / `last_known_trays`
    report for that printer."""
    states: dict[int, dict] = {}
    mock = MagicMock()
    mock.get_status.side_effect = lambda pid: _status(states[pid]["raw_data"]) if pid in states else None
    mock.is_connected.side_effect = lambda pid: states.get(pid, {}).get("connected", False)
    mock.is_print_active.side_effect = lambda pid: states.get(pid, {}).get("print_active", False)
    mock.last_known_trays.side_effect = lambda pid: states.get(pid, {}).get("last_known", {})
    with patch("backend.app.services.auto_printer_select.printer_manager", mock):
        yield states


async def _queue_item(db_session, *, printer_id: int, status: str = "pending") -> None:
    db_session.add(PrintQueueItem(printer_id=printer_id, status=status))
    await db_session.commit()


# --- no candidates -----------------------------------------------------------


class TestNoCandidates:
    @pytest.mark.asyncio
    async def test_no_active_printers_raises(self, db_session, pm):
        with pytest.raises(aps.NoPrinterAvailableError, match="No active printers"):
            await aps.select_printer(db_session, filament_type="PETG", color_hex=None, model_size_mm=None)

    @pytest.mark.asyncio
    async def test_no_printer_has_filament_type_raises(self, db_session, pm, printer_factory):
        printer = await printer_factory(model="X1C")
        pm[printer.id] = {
            "connected": True,
            "print_active": False,
            "raw_data": _raw_data((0, [_ams_tray(0, 0, "PLA", "FFFFFFFF")])),
        }
        with pytest.raises(aps.NoPrinterAvailableError, match="PETG"):
            await aps.select_printer(db_session, filament_type="PETG", color_hex=None, model_size_mm=None)

    @pytest.mark.asyncio
    async def test_wrong_color_raises_mentioning_colour(self, db_session, pm, printer_factory):
        printer = await printer_factory(model="X1C")
        pm[printer.id] = {
            "connected": True,
            "print_active": False,
            "raw_data": _raw_data((0, [_ams_tray(0, 0, "PETG", "FF0000FF")])),  # red
        }
        with pytest.raises(aps.NoPrinterAvailableError, match="#0000FF"):
            await aps.select_printer(
                db_session,
                filament_type="PETG",
                color_hex="#0000FF",
                model_size_mm=None,  # blue
            )


# --- ranking -------------------------------------------------------------


class TestRanking:
    @pytest.mark.asyncio
    async def test_prefers_idle_over_busy(self, db_session, pm, printer_factory):
        idle = await printer_factory(model="X1C", name="Idle One")
        busy = await printer_factory(model="X1C", name="Busy One")
        loaded = _raw_data((0, [_ams_tray(0, 0, "PETG", "0A6CF5FF")]))
        pm[idle.id] = {"connected": True, "print_active": False, "raw_data": loaded}
        pm[busy.id] = {"connected": True, "print_active": True, "raw_data": loaded}

        result = await aps.select_printer(db_session, filament_type="PETG", color_hex="#0A6CF5", model_size_mm=None)
        assert result.printer.id == idle.id
        assert result.reason.startswith("Idle,")
        assert result.tray is not None
        assert result.tray.filament_type == "PETG"

    @pytest.mark.asyncio
    async def test_prefers_shortest_pending_queue_among_busy(self, db_session, pm, printer_factory):
        p1 = await printer_factory(model="X1C", name="Busy Long Queue")
        p2 = await printer_factory(model="X1C", name="Busy Short Queue")
        loaded = _raw_data((0, [_ams_tray(0, 0, "PETG", "0A6CF5FF")]))
        pm[p1.id] = {"connected": True, "print_active": True, "raw_data": loaded}
        pm[p2.id] = {"connected": True, "print_active": True, "raw_data": loaded}

        await _queue_item(db_session, printer_id=p1.id)
        await _queue_item(db_session, printer_id=p1.id)
        await _queue_item(db_session, printer_id=p2.id)

        result = await aps.select_printer(db_session, filament_type="PETG", color_hex=None, model_size_mm=None)
        assert result.printer.id == p2.id
        assert "Shortest queue" in result.reason
        assert "1 pending" in result.reason

    @pytest.mark.asyncio
    async def test_offline_printer_is_last_resort_but_still_selected(self, db_session, pm, printer_factory):
        printer = await printer_factory(model="X1C")
        pm[printer.id] = {
            "connected": False,
            "print_active": False,
            "raw_data": {},
            "last_known": _raw_data((0, [_ams_tray(0, 0, "PETG", "0A6CF5FF")])),
        }
        result = await aps.select_printer(db_session, filament_type="PETG", color_hex="#0A6CF5", model_size_mm=None)
        assert result.printer.id == printer.id
        assert result.reason.startswith("Offline,")

    @pytest.mark.asyncio
    async def test_color_within_threshold_still_matches(self, db_session, pm, printer_factory):
        """colors_similar's tolerance (spec: ~40/255) — a spool read back
        slightly differently by the AMS sensor must still count."""
        printer = await printer_factory(model="X1C")
        # Requested #0A6CF5 vs loaded #146DF0 — small RGB deltas, well under
        # the threshold.
        pm[printer.id] = {
            "connected": True,
            "print_active": False,
            "raw_data": _raw_data((0, [_ams_tray(0, 0, "PETG", "146DF0FF")])),
        }
        result = await aps.select_printer(db_session, filament_type="PETG", color_hex="#0A6CF5", model_size_mm=None)
        assert result.printer.id == printer.id


# --- explicit override ----------------------------------------------------


class TestExplicitOverride:
    @pytest.mark.asyncio
    async def test_explicit_printer_not_found_raises(self, db_session, pm):
        with pytest.raises(aps.NoPrinterAvailableError):
            await aps.select_printer(
                db_session,
                filament_type="PETG",
                color_hex=None,
                model_size_mm=None,
                explicit_printer_id=999999,
            )

    @pytest.mark.asyncio
    async def test_explicit_printer_with_no_matching_tray_returns_none_tray(self, db_session, pm, printer_factory):
        printer = await printer_factory(model="X1C")
        pm[printer.id] = {
            "connected": True,
            "print_active": False,
            "raw_data": _raw_data((0, [_ams_tray(0, 0, "PLA", "FFFFFFFF")])),
        }
        result = await aps.select_printer(
            db_session,
            filament_type="PETG",
            color_hex=None,
            model_size_mm=None,
            explicit_printer_id=printer.id,
        )
        assert result.printer.id == printer.id
        assert result.tray is None
        assert "Manually selected" in result.reason

    @pytest.mark.asyncio
    async def test_explicit_printer_with_matching_tray(self, db_session, pm, printer_factory):
        printer = await printer_factory(model="X1C")
        pm[printer.id] = {
            "connected": True,
            "print_active": False,
            "raw_data": _raw_data((0, [_ams_tray(0, 2, "PETG", "0A6CF5FF")])),
        }
        result = await aps.select_printer(
            db_session,
            filament_type="PETG",
            color_hex="#0A6CF5",
            model_size_mm=None,
            explicit_printer_id=printer.id,
        )
        assert result.tray is not None
        assert result.tray.ams_id == 0
        assert result.tray.tray_id == 2
        assert result.tray.global_tray_id == 2  # ams_id * 4 + tray_id


# --- build volume ----------------------------------------------------------


class TestBuildVolume:
    @pytest.mark.asyncio
    async def test_model_too_large_for_every_bed_raises(self, db_session, pm, printer_factory):
        printer = await printer_factory(model="X1C")
        pm[printer.id] = {
            "connected": True,
            "print_active": False,
            "raw_data": _raw_data((0, [_ams_tray(0, 0, "PETG", "0A6CF5FF")])),
        }
        with (
            patch(
                "backend.app.services.auto_printer_select.get_build_volume",
                AsyncMock(return_value=(100.0, 100.0, 100.0)),
            ),
            pytest.raises(aps.NoPrinterAvailableError, match="larger than the print bed"),
        ):
            await aps.select_printer(
                db_session,
                filament_type="PETG",
                color_hex=None,
                model_size_mm=(300.0, 300.0, 300.0),
            )

    @pytest.mark.asyncio
    async def test_model_fits_one_of_several_beds(self, db_session, pm, printer_factory):
        # Different models (not just different printers of the same model),
        # since the build-volume cache is keyed by machine_key — two X1Cs
        # share one bed size for real, so distinguishing them requires
        # distinguishing the printer *model*, exactly as production does.
        small = await printer_factory(model="A1 Mini", name="Small Bed")
        big = await printer_factory(model="X1C", name="Big Bed")
        loaded = _raw_data((0, [_ams_tray(0, 0, "PETG", "0A6CF5FF")]))
        pm[small.id] = {"connected": True, "print_active": False, "raw_data": loaded}
        pm[big.id] = {"connected": True, "print_active": False, "raw_data": loaded}

        async def fake_get_build_volume(_svc, *, machine_key):
            if "A1" in machine_key:
                return (100.0, 100.0, 100.0)
            return (350.0, 350.0, 350.0)

        with patch(
            "backend.app.services.auto_printer_select.get_build_volume",
            side_effect=fake_get_build_volume,
        ):
            result = await aps.select_printer(
                db_session,
                filament_type="PETG",
                color_hex=None,
                model_size_mm=(200.0, 200.0, 200.0),
            )
        assert result.printer.id == big.id

    @pytest.mark.asyncio
    async def test_unresolvable_build_volume_fails_open(self, db_session, pm, printer_factory):
        printer = await printer_factory(model="X1C")
        pm[printer.id] = {
            "connected": True,
            "print_active": False,
            "raw_data": _raw_data((0, [_ams_tray(0, 0, "PETG", "0A6CF5FF")])),
        }
        with patch(
            "backend.app.services.auto_printer_select.get_build_volume",
            AsyncMock(side_effect=PresetSelectionError("sidecar unreachable")),
        ):
            result = await aps.select_printer(
                db_session,
                filament_type="PETG",
                color_hex=None,
                model_size_mm=(200.0, 200.0, 200.0),
            )
        assert result.printer.id == printer.id

    @pytest.mark.asyncio
    async def test_unrecognised_model_skips_build_volume_check(self, db_session, pm, printer_factory):
        printer = await printer_factory(model="SomeUnknownPrinter")
        pm[printer.id] = {
            "connected": True,
            "print_active": False,
            "raw_data": _raw_data((0, [_ams_tray(0, 0, "PETG", "0A6CF5FF")])),
        }
        result = await aps.select_printer(
            db_session,
            filament_type="PETG",
            color_hex=None,
            model_size_mm=(200.0, 200.0, 200.0),
        )
        assert result.printer.id == printer.id


# --- model_long_name ---------------------------------------------------------


class TestModelLongName:
    def test_known_short_codes(self):
        assert aps.model_long_name("X1C") == "Bambu Lab X1 Carbon"
        assert aps.model_long_name("P1S") == "Bambu Lab P1S"
        # Verified against a live sidecar's /profiles/bundled: lowercase
        # "mini", not the capitalised form Bambuddy normalizes model names to.
        assert aps.model_long_name("A1 Mini") == "Bambu Lab A1 mini"

    def test_unknown_short_code_returns_none(self):
        assert aps.model_long_name("Some New Printer") is None

    def test_none_returns_none(self):
        assert aps.model_long_name(None) is None
