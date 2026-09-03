"""Tests for the auto-print pipeline: schemas, `auto_print_options.py`,
`mock_printer_state.py`, the `/auto-print/*` routes, and the end-to-end
background worker in `auto_print_flow.py`.

Two different DB-access strategies are used deliberately:

- Route-shape tests (permission gate, 202/404 responses, `/options` JSON
  shape) go through `async_client`, which is wired to `test_engine` via the
  `get_db` dependency override.
- Worker/flow tests call `auto_print_flow.start_flow()` directly with a
  session factory built from the SAME `test_engine` fixture `printer_factory`
  uses, rather than going through `POST /auto-print/`. This sidesteps a
  pre-existing split in this test suite: the background task's own DB
  session comes from `backend.app.core.database.async_session` (the
  production module-level sessionmaker, pointed at the suite's disposable
  *file* database — see conftest.py's own comment on this), which is a
  *different* SQLite database than `test_engine`'s in-memory one that
  `printer_factory` and `db_session` write into. Calling `start_flow`
  directly with a `test_engine`-backed session factory keeps everything the
  worker reads/writes in the same database the test fixtures populate,
  without touching that pre-existing split (not something this feature
  should be reworking).

Live network calls to a slicer sidecar are never made: `slicer_api.
set_shared_http_client` swaps in an `httpx.MockTransport` for every test
that needs one, mirroring `tests/integration/test_library_slice_api.py`'s
own pattern for the same reused `slice_and_persist` code path.
"""

from __future__ import annotations

import io
import json
import zipfile
from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings as app_settings
from backend.app.schemas.auto_print import AutoPrintFlow, AutoPrintRequest, PresetChoice
from backend.app.services import auto_print_flow, slicer_api as slicer_api_module

# ---------------------------------------------------------------------------
# A tiny, valid, non-degenerate ASCII STL (one tetrahedron) — small enough to
# embed inline, real enough for trimesh to load and report a bounding box.
# ---------------------------------------------------------------------------
_TEST_STL = b"""solid test
facet normal 0 0 1
  outer loop
    vertex 0 0 0
    vertex 1 0 0
    vertex 0 1 0
  endloop
endfacet
facet normal 0 0 -1
  outer loop
    vertex 0 0 0
    vertex 0 1 0
    vertex 0 0 1
  endloop
endfacet
facet normal 1 0 0
  outer loop
    vertex 1 0 0
    vertex 0 0 1
    vertex 0 1 0
  endloop
endfacet
facet normal 0 -1 0
  outer loop
    vertex 0 0 0
    vertex 0 0 1
    vertex 1 0 0
  endloop
endfacet
endsolid test
"""

_BUNDLED_PAYLOAD = {
    "printer": [{"name": "Bambu Lab X1 Carbon 0.4 nozzle", "base_id": "m1"}],
    "process": [
        {
            "name": "0.20mm Standard @BBL X1C",
            "base_id": "p1",
            "compatible_printers": ["Bambu Lab X1 Carbon 0.4 nozzle"],
        }
    ],
    "filament": [
        {
            "name": "Bambu PETG Basic @BBL X1C",
            "base_id": "f1",
            "compatible_printers": ["Bambu Lab X1 Carbon 0.4 nozzle"],
            "filament_type": "PETG",
            "filament_colour": None,
        }
    ],
}


def _resolve_profile_body(category: str | None) -> dict:
    if category == "machine":
        return {"printable_area": ["0x0", "256x0", "256x256", "0x256"], "printable_height": "250"}
    if category == "filament":
        # PETG per docs/auto-print-pipeline-spec.md's own measured table:
        # cool_plate_temp=0 (unsupported), everything else non-zero.
        return {
            "cool_plate_temp": "0",
            "eng_plate_temp": "70",
            "hot_plate_temp": "70",
            "textured_plate_temp": "70",
            "supertack_plate_temp": "70",
        }
    return {}


def _make_sliced_output() -> bytes:
    """A minimal sliced-output 3MF, matching the shape
    `test_library_slice_api.py`'s own `_make_single_plate_sliced_output`
    uses: no `Metadata/project_settings.config`, so `start_gcode_is_missing`
    finds nothing to check and no-ops rather than rejecting the (deliberately
    start-gcode-free) test fixture."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/model_settings.config", "<config/>")
        zf.writestr("Metadata/plate_1.gcode", b"; test gcode\nG28\n")
    return buf.getvalue()


def _sidecar_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/profiles/bundled"):
        return httpx.Response(200, json=_BUNDLED_PAYLOAD)
    if path.endswith("/profiles/resolve"):
        payload = json.loads(request.content or b"{}")
        return httpx.Response(200, json={"profile": _resolve_profile_body(payload.get("category"))})
    if "/slice/progress/" in path:
        return httpx.Response(404)
    if path.endswith("/slice"):
        return httpx.Response(
            200,
            content=_make_sliced_output(),
            headers={
                "x-print-time-seconds": "1746",
                "x-filament-used-g": "4.43",
                "x-filament-used-mm": "1474.71",
            },
        )
    return httpx.Response(404)


@pytest.fixture
def mock_sidecar():
    """Route every `SlicerApiService` call in this test to `_sidecar_handler`
    instead of a real network connection — same technique
    `test_library_slice_api.py` uses for the same underlying `slice_and_persist`."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(_sidecar_handler), timeout=10.0)
    slicer_api_module.set_shared_http_client(client)
    yield
    slicer_api_module.set_shared_http_client(None)


@pytest.fixture
def flow_session_factory(test_engine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the SAME `test_engine` `printer_factory` /
    `db_session` write into — see the module docstring for why this is used
    instead of `backend.app.core.database.async_session` when calling
    `start_flow` directly."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def _make_upload(filename: str = "bracket.stl", content: bytes = _TEST_STL):
    from starlette.datastructures import UploadFile

    return UploadFile(file=io.BytesIO(content), filename=filename, size=len(content))


async def _poll_flow(flow_id: int, *, timeout: float = 10.0) -> AutoPrintFlow:
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        flow = auto_print_flow.get_flow(flow_id)
        assert flow is not None, "flow disappeared from the registry mid-poll"
        if flow.stage in ("queued", "failed"):
            return flow
        await asyncio.sleep(0.05)
    raise AssertionError(f"auto-print flow {flow_id} did not reach a terminal stage within {timeout}s")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TestAutoPrintSchemas:
    def test_request_defaults(self):
        req = AutoPrintRequest(filament_type="PETG")
        assert req.color_hex is None
        assert req.quality == "Standard"
        assert req.layer_height == 0.20
        assert req.printer_id is None
        assert req.auto_orient is True
        assert req.auto_arrange is True

    def test_preset_choice_requires_bed_type(self):
        with pytest.raises(ValidationError):
            PresetChoice(printer="p", process="q", filament="f")  # missing bed_type

        choice = PresetChoice(printer="p", process="q", filament="f", bed_type="Textured PEI Plate")
        assert choice.bed_type == "Textured PEI Plate"

    def test_flow_defaults(self):
        flow = AutoPrintFlow(id=1, stage="pending")
        assert flow.progress == 0
        assert flow.error is None
        assert flow.printer is None
        assert flow.presets is None
        assert flow.estimate.print_time_seconds is None
        assert flow.model_preview_url is None
        assert flow.gcode_preview_url is None


# ---------------------------------------------------------------------------
# mock_printer_state.py
# ---------------------------------------------------------------------------


class TestMockPrinterState:
    """Both cases patch a *fresh* `PrinterManager` instance in for the
    module attribute `mock_printer_state.install_mock_printer_state` reads
    at call time (its import of `printer_manager` is function-local, so this
    redirection works) — never the real process-wide singleton, so nothing
    here can leak into other tests."""

    def test_noop_when_env_var_absent(self, monkeypatch):
        from backend.app.services.mock_printer_state import install_mock_printer_state
        from backend.app.services.printer_manager import PrinterManager

        monkeypatch.delenv("BAMBUDDY_MOCK_PRINTER_STATE", raising=False)
        fresh = PrinterManager()
        with patch("backend.app.services.printer_manager.printer_manager", fresh):
            install_mock_printer_state()
            assert fresh.is_connected(999) is False
            assert fresh.get_status(999) is None

    def test_installs_canned_state_when_enabled(self, monkeypatch):
        from backend.app.services.mock_printer_state import install_mock_printer_state
        from backend.app.services.printer_manager import PrinterManager

        monkeypatch.setenv("BAMBUDDY_MOCK_PRINTER_STATE", "1")
        fresh = PrinterManager()
        with patch("backend.app.services.printer_manager.printer_manager", fresh):
            install_mock_printer_state(allow_under_pytest=True)
            assert fresh.is_connected(999) is True
            state = fresh.get_status(999)
            assert state is not None
            assert state.connected is True
            assert state.state == "IDLE"

            trays = [t for unit in state.raw_data["ams"] for t in unit["tray"]]
            petg = [t for t in trays if t.get("tray_type") == "PETG"]
            assert petg, "mock AMS state must include a PETG tray"
            assert petg[0]["tray_color"].upper().startswith("0A6CF5")

            pla_colors = {t["tray_color"] for t in trays if t.get("tray_type") == "PLA"}
            assert len(pla_colors) >= 2, "mock AMS state must include PLA in at least two colours"

            # Idempotent: a second install on the same instance changes nothing further.
            install_mock_printer_state(allow_under_pytest=True)
            assert fresh.is_connected(999) is True

    def test_last_known_trays_also_mocked(self, monkeypatch):
        from backend.app.services.mock_printer_state import install_mock_printer_state
        from backend.app.services.printer_manager import PrinterManager

        monkeypatch.setenv("BAMBUDDY_MOCK_PRINTER_STATE", "1")
        fresh = PrinterManager()
        with patch("backend.app.services.printer_manager.printer_manager", fresh):
            install_mock_printer_state(allow_under_pytest=True)
            raw = fresh.last_known_trays(123)
            assert raw.get("ams"), "last_known_trays must also report the canned AMS state"


# ---------------------------------------------------------------------------
# auto_print_options.py
# ---------------------------------------------------------------------------


class TestAutoPrintOptions:
    @pytest.mark.asyncio
    async def test_default_options(self, db_session):
        from backend.app.services.auto_print_options import default_options

        assert await default_options(db_session) == {"quality": "Standard", "layer_height": 0.20}

    @pytest.mark.asyncio
    async def test_list_quality_tiers_empty_when_sidecar_unreachable(self, db_session, monkeypatch):
        from backend.app.services.auto_print_options import list_quality_tiers

        # Deterministically unreachable regardless of what's actually running
        # on the host — a real dev sidecar on :3001 would otherwise make this
        # test's result depend on the environment it happens to run in.
        monkeypatch.setattr(app_settings, "bambu_studio_api_url", "http://127.0.0.1:1")
        tiers = await list_quality_tiers(db_session)
        assert tiers == []

    @pytest.mark.asyncio
    async def test_list_quality_tiers_uses_sidecar(self, db_session, mock_sidecar):
        from backend.app.services.auto_print_options import list_quality_tiers

        tiers = await list_quality_tiers(db_session)
        assert {"tier": "Standard", "layer_heights": [0.2]} in tiers

    @pytest.mark.asyncio
    async def test_list_loaded_filaments_reads_live_ams_state(self, db_session, printer_factory, monkeypatch):
        from backend.app.services.auto_print_options import list_loaded_filaments
        from backend.app.services.bambu_mqtt import PrinterState
        from backend.app.services.printer_manager import printer_manager as real_printer_manager

        printer = await printer_factory(name="Farm X1C", model="X1C")

        state = PrinterState(
            connected=True,
            state="IDLE",
            raw_data={
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_type": "PETG", "tray_color": "0A6CF5FF", "tray_id_name": "Blue"},
                            {"id": 1, "tray_type": "PLA", "tray_color": "1A1A1AFF", "tray_id_name": "Black"},
                        ],
                    }
                ],
                "vt_tray": [],
            },
        )
        # Monkeypatch attributes directly on the real, shared singleton
        # instance (auto-reverted by `monkeypatch` at teardown) rather than
        # replacing the module-level name — `auto_print_options.py` already
        # holds its own `from ... import printer_manager` reference, which a
        # module-attribute patch would not redirect (see TestMockPrinterState
        # for why that technique is used there instead).
        monkeypatch.setattr(real_printer_manager, "is_connected", lambda pid: True)
        monkeypatch.setattr(real_printer_manager, "get_status", lambda pid: state)
        monkeypatch.setattr(real_printer_manager, "last_known_trays", lambda pid: state.raw_data)

        filaments = await list_loaded_filaments(db_session)
        combos = {(f.filament_type, f.color_hex) for f in filaments}
        assert ("PETG", "#0A6CF5") in combos
        assert ("PLA", "#1A1A1A") in combos

        petg = next(f for f in filaments if f.filament_type == "PETG")
        assert petg.printer_ids == [printer.id]
        assert petg.color_name == "Blue"


# ---------------------------------------------------------------------------
# Routes — shape, permission gate, ownership scoping
# ---------------------------------------------------------------------------


class TestAutoPrintRoutes:
    @pytest.mark.asyncio
    async def test_options_route_shape(self, async_client: AsyncClient):
        resp = await async_client.get("/api/v1/auto-print/options")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) == {"filaments", "quality_tiers", "defaults"}
        assert body["defaults"] == {"quality": "Standard", "layer_height": 0.20}

    @pytest.mark.asyncio
    async def test_get_unknown_flow_404s(self, async_client: AsyncClient):
        resp = await async_client.get("/api/v1/auto-print/999999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_post_returns_202_pending_immediately(self, async_client: AsyncClient):
        resp = await async_client.post(
            "/api/v1/auto-print/",
            files={"file": ("bracket.stl", _TEST_STL, "model/stl")},
            data={"filament_type": "PETG", "quality": "Standard", "layer_height": "0.20"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["stage"] == "pending"
        assert isinstance(body["id"], int)

        # Drain the background task to completion before returning. conftest's
        # `event_loop` fixture is session-scoped, so a task left running here
        # (this route's worker uses the production `async_session`, not
        # `test_engine`, and has no printers to find — it will land on
        # stage='failed' quickly) would otherwise keep executing during later
        # tests on the same loop and could race them.
        import asyncio

        flow_id = body["id"]
        for _ in range(100):
            poll = await async_client.get(f"/api/v1/auto-print/{flow_id}")
            if poll.json()["stage"] in ("queued", "failed"):
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("auto-print flow did not reach a terminal stage")


# ---------------------------------------------------------------------------
# The background worker, end to end
# ---------------------------------------------------------------------------


class TestAutoPrintFlowWorker:
    @pytest.mark.asyncio
    async def test_fails_with_no_active_printers(self, flow_session_factory):
        """No `Printer` rows at all — `select_printer` should reject this
        before ever touching the (unmocked, in this test) slicer sidecar,
        and the flow must land on `stage='failed'` with a short, non-
        traceback error rather than raising out of the background task."""
        flow_id = await auto_print_flow.start_flow(
            flow_session_factory,
            upload=_make_upload(),
            request=AutoPrintRequest(filament_type="PETG", color_hex="#0A6CF5"),
            current_user=None,
        )
        flow = await _poll_flow(flow_id)
        assert flow.stage == "failed"
        assert flow.error
        assert "Traceback" not in flow.error
        assert 'File "' not in flow.error
        assert flow.printer is None
        assert flow.queue_item_id is None

    @pytest.mark.asyncio
    async def test_happy_path_reaches_queued(
        self, flow_session_factory, printer_factory, monkeypatch, mock_sidecar, tmp_path
    ):
        """Upload -> analyse -> pick the one idle printer with PETG loaded ->
        slice against a mocked sidecar -> land in the print queue. Exercises
        `auto_print_flow.py`'s full stage sequence and its integration with
        the real `auto_preset_select` / `auto_printer_select` modules."""
        from backend.app.services.bambu_mqtt import PrinterState
        from backend.app.services.printer_manager import printer_manager as real_printer_manager

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)

        printer = await printer_factory(name="Farm X1C", model="X1C")

        state = PrinterState(
            connected=True,
            state="IDLE",
            raw_data={
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            {"id": 0, "tray_type": "PETG", "tray_color": "0A6CF5FF", "tray_id_name": "Blue"},
                        ],
                    }
                ],
                "vt_tray": [],
            },
        )
        monkeypatch.setattr(real_printer_manager, "is_connected", lambda pid: True)
        monkeypatch.setattr(real_printer_manager, "get_status", lambda pid: state)
        monkeypatch.setattr(real_printer_manager, "last_known_trays", lambda pid: state.raw_data)
        monkeypatch.setattr(real_printer_manager, "is_awaiting_plate_clear", lambda pid: False)

        flow_id = await auto_print_flow.start_flow(
            flow_session_factory,
            upload=_make_upload(),
            request=AutoPrintRequest(filament_type="PETG", color_hex="#0A6CF5"),
            current_user=None,
        )
        flow = await _poll_flow(flow_id, timeout=15.0)

        assert flow.stage == "queued", flow.error
        assert flow.queue_item_id is not None
        assert flow.library_file_id is not None
        assert flow.sliced_library_file_id is not None
        assert flow.progress == 100

        assert flow.printer is not None
        assert flow.printer.id == printer.id
        assert flow.printer.model == "X1C"
        assert "PETG" in flow.printer.reason

        assert flow.presets is not None
        assert flow.presets.printer == "Bambu Lab X1 Carbon 0.4 nozzle"
        assert flow.presets.process == "0.20mm Standard @BBL X1C"
        assert flow.presets.filament == "Bambu PETG Basic @BBL X1C"
        # PETG's cool_plate_temp is mocked as 0 (unsupported) and
        # textured_plate_temp as non-zero, so Textured PEI Plate must win.
        assert flow.presets.bed_type == "Textured PEI Plate"

        assert flow.estimate.print_time_seconds == 1746
        assert flow.estimate.filament_used_g == 4.43

        assert flow.model_preview_url == f"/api/v1/library/files/{flow.library_file_id}/download"
        assert flow.gcode_preview_url == f"/api/v1/library/files/{flow.sliced_library_file_id}/gcode"
