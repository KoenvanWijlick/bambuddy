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

import asyncio
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


_TERMINAL_STAGES = ("queued", "failed", "awaiting_approval", "discarded")


async def _poll_flow(flow_id: int, *, timeout: float = 10.0) -> AutoPrintFlow:
    """Poll until the flow reaches a stage the worker itself never advances
    out of on its own — includes `awaiting_approval` (the worker pauses
    there and waits for a separate `approve`/`discard` call) alongside the
    older `queued`/`failed` terminal stages, so tests exercising the
    approval gate can poll with the same helper as tests that don't.
    """
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        flow = auto_print_flow.get_flow(flow_id)
        assert flow is not None, "flow disappeared from the registry mid-poll"
        if flow.stage in _TERMINAL_STAGES:
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
            PresetChoice(printer="p", process="q", filament="f", brim="Off")  # missing bed_type

        choice = PresetChoice(
            printer="p", process="q", filament="f", bed_type="Textured PEI Plate", brim="Inner + outer, 5 mm"
        )
        assert choice.bed_type == "Textured PEI Plate"
        assert choice.brim == "Inner + outer, 5 mm"

    def test_request_brim_defaults_on(self):
        req = AutoPrintRequest(filament_type="PETG")
        assert req.require_approval is True
        assert req.brim is True
        assert req.brim_width == 5.0

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
            if poll.json()["stage"] in _TERMINAL_STAGES:
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
        the real `auto_preset_select` / `auto_printer_select` modules.

        Passes `require_approval=False` deliberately: this test predates the
        Round 2 approval gate and its whole point is the one-shot
        slice-then-queue path, which is exactly what `require_approval=False`
        is for keeping reachable. The approval-gate-ON path (the new
        default) gets its own coverage in `TestApprovalGate` below.
        """
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
            request=AutoPrintRequest(filament_type="PETG", color_hex="#0A6CF5", require_approval=False),
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
        # brim defaults to True/5mm on AutoPrintRequest.
        assert flow.presets.brim == "Inner + outer, 5 mm"

        assert flow.estimate.print_time_seconds == 1746
        assert flow.estimate.filament_used_g == 4.43

        assert flow.model_preview_url == f"/api/v1/library/files/{flow.library_file_id}/download"
        assert flow.gcode_preview_url == f"/api/v1/library/files/{flow.sliced_library_file_id}/gcode"


# ---------------------------------------------------------------------------
# `_patch_process_brim` (library.py) — direct unit coverage, mirroring how
# `_patch_process_bed_type` would be tested if it had its own dedicated
# suite. Lives here rather than in test_library_slice_api.py because this
# function was added specifically for the auto-print brim override.
# ---------------------------------------------------------------------------


class TestPatchProcessBrim:
    def test_patches_both_fields_and_writes_width_as_string(self):
        from backend.app.api.routes.library import _patch_process_brim

        original = json.dumps({"brim_type": "auto_brim", "brim_width": "5", "brim_object_gap": "0.1", "other": "x"})
        patched = json.loads(_patch_process_brim(original, "outer_and_inner", 5.0))

        assert patched["brim_type"] == "outer_and_inner"
        assert patched["brim_width"] == "5"
        assert isinstance(patched["brim_width"], str)
        # brim_object_gap must be left alone (spec: "Leave brim_object_gap alone").
        assert patched["brim_object_gap"] == "0.1"
        assert patched["other"] == "x"

    def test_each_field_patched_independently(self):
        from backend.app.api.routes.library import _patch_process_brim

        original = json.dumps({"brim_type": "auto_brim", "brim_width": "5"})

        only_type = json.loads(_patch_process_brim(original, "no_brim", None))
        assert only_type == {"brim_type": "no_brim", "brim_width": "5"}

        only_width = json.loads(_patch_process_brim(original, None, 3.5))
        assert only_width == {"brim_type": "auto_brim", "brim_width": "3.5"}

    def test_unparseable_json_returned_unchanged(self):
        from backend.app.api.routes.library import _patch_process_brim

        assert _patch_process_brim("not json", "outer_and_inner", 5.0) == "not json"

    def test_non_dict_json_returned_unchanged(self):
        from backend.app.api.routes.library import _patch_process_brim

        assert _patch_process_brim("[1, 2, 3]", "outer_and_inner", 5.0) == "[1, 2, 3]"


# ---------------------------------------------------------------------------
# Round 2 — approval gate
# ---------------------------------------------------------------------------


async def _queue_item_count(db_session: AsyncSession) -> int:
    from sqlalchemy import func, select

    from backend.app.models.print_queue import PrintQueueItem

    result = await db_session.execute(select(func.count()).select_from(PrintQueueItem))
    return result.scalar_one()


async def _start_paused_flow(
    flow_session_factory,
    printer_factory,
    monkeypatch,
    tmp_path,
    *,
    require_approval: bool = True,
    brim: bool = True,
    brim_width: float = 5.0,
):
    """Shared setup for approval-gate tests: one idle X1C with PETG loaded —
    the exact same printer/AMS shape `TestAutoPrintFlowWorker.
    test_happy_path_reaches_queued` uses — parameterised on the
    approval/brim request fields under test here. Returns the started flow's
    id; the caller polls it themselves since different tests want to stop at
    different stages.
    """
    from backend.app.services.bambu_mqtt import PrinterState
    from backend.app.services.printer_manager import printer_manager as real_printer_manager

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)

    await printer_factory(name="Farm X1C", model="X1C")

    state = PrinterState(
        connected=True,
        state="IDLE",
        raw_data={
            "ams": [
                {
                    "id": 0,
                    "tray": [{"id": 0, "tray_type": "PETG", "tray_color": "0A6CF5FF", "tray_id_name": "Blue"}],
                }
            ],
            "vt_tray": [],
        },
    )
    monkeypatch.setattr(real_printer_manager, "is_connected", lambda pid: True)
    monkeypatch.setattr(real_printer_manager, "get_status", lambda pid: state)
    monkeypatch.setattr(real_printer_manager, "last_known_trays", lambda pid: state.raw_data)
    monkeypatch.setattr(real_printer_manager, "is_awaiting_plate_clear", lambda pid: False)

    return await auto_print_flow.start_flow(
        flow_session_factory,
        upload=_make_upload(),
        request=AutoPrintRequest(
            filament_type="PETG",
            color_hex="#0A6CF5",
            require_approval=require_approval,
            brim=brim,
            brim_width=brim_width,
        ),
        current_user=None,
    )


class TestApprovalGate:
    @pytest.mark.asyncio
    async def test_pauses_before_queueing(
        self, flow_session_factory, printer_factory, monkeypatch, mock_sidecar, tmp_path, db_session
    ):
        """The whole point of the pause: by the time the flow reaches
        `awaiting_approval`, the sliced file/preview/estimate are already
        there, but NOTHING has been queued yet."""
        flow_id = await _start_paused_flow(flow_session_factory, printer_factory, monkeypatch, tmp_path)
        flow = await _poll_flow(flow_id, timeout=15.0)

        assert flow.stage == "awaiting_approval", flow.error
        assert flow.progress == 85
        assert flow.queue_item_id is None
        assert flow.sliced_library_file_id is not None
        assert flow.gcode_preview_url is not None
        assert flow.estimate.print_time_seconds is not None
        assert flow.estimate.filament_used_g is not None

        assert await _queue_item_count(db_session) == 0

    @pytest.mark.asyncio
    async def test_approve_creates_exactly_one_queue_item(
        self, flow_session_factory, printer_factory, monkeypatch, mock_sidecar, tmp_path, db_session
    ):
        flow_id = await _start_paused_flow(flow_session_factory, printer_factory, monkeypatch, tmp_path)
        paused = await _poll_flow(flow_id, timeout=15.0)
        assert paused.stage == "awaiting_approval", paused.error

        approved = await auto_print_flow.approve_flow(flow_session_factory, flow_id)
        assert approved is not None
        assert approved.stage == "queued", approved.error
        assert approved.queue_item_id is not None
        assert approved.progress == 100

        assert await _queue_item_count(db_session) == 1

    @pytest.mark.asyncio
    async def test_discard_creates_no_queue_item(
        self, flow_session_factory, printer_factory, monkeypatch, mock_sidecar, tmp_path, db_session
    ):
        flow_id = await _start_paused_flow(flow_session_factory, printer_factory, monkeypatch, tmp_path)
        paused = await _poll_flow(flow_id, timeout=15.0)
        assert paused.stage == "awaiting_approval", paused.error

        discarded = await auto_print_flow.discard_flow(flow_id)
        assert discarded is not None
        assert discarded.stage == "discarded"
        assert discarded.queue_item_id is None
        # The sliced result is left in place — only queueing is skipped.
        assert discarded.sliced_library_file_id == paused.sliced_library_file_id

        assert await _queue_item_count(db_session) == 0

    @pytest.mark.asyncio
    async def test_approve_from_wrong_stage_conflicts(self, flow_session_factory):
        """No printers at all -> the flow fails fast, well before
        `awaiting_approval`. Approving (or discarding) it must raise rather
        than silently no-op or queue something."""
        flow_id = await auto_print_flow.start_flow(
            flow_session_factory,
            upload=_make_upload(),
            request=AutoPrintRequest(filament_type="PETG", color_hex="#0A6CF5"),
            current_user=None,
        )
        flow = await _poll_flow(flow_id, timeout=15.0)
        assert flow.stage == "failed"

        with pytest.raises(auto_print_flow.FlowStageConflictError) as exc_info:
            await auto_print_flow.approve_flow(flow_session_factory, flow_id)
        assert "failed" in str(exc_info.value)
        assert exc_info.value.stage == "failed"

    @pytest.mark.asyncio
    async def test_discard_from_wrong_stage_conflicts(self, flow_session_factory):
        flow_id = await auto_print_flow.start_flow(
            flow_session_factory,
            upload=_make_upload(),
            request=AutoPrintRequest(filament_type="PETG", color_hex="#0A6CF5"),
            current_user=None,
        )
        flow = await _poll_flow(flow_id, timeout=15.0)
        assert flow.stage == "failed"

        with pytest.raises(auto_print_flow.FlowStageConflictError):
            await auto_print_flow.discard_flow(flow_id)

    @pytest.mark.asyncio
    async def test_approve_and_discard_409_over_http(self, async_client: AsyncClient):
        """Route-level check that `FlowStageConflictError` becomes a 409
        with a readable message, using the same no-printers-available flow
        (fails fast, never reaches `awaiting_approval`) as the direct-call
        version above."""
        resp = await async_client.post(
            "/api/v1/auto-print/",
            files={"file": ("bracket.stl", _TEST_STL, "model/stl")},
            data={"filament_type": "PETG"},
        )
        flow_id = resp.json()["id"]

        for _ in range(100):
            poll = await async_client.get(f"/api/v1/auto-print/{flow_id}")
            if poll.json()["stage"] in _TERMINAL_STAGES:
                break
            await asyncio.sleep(0.05)
        assert poll.json()["stage"] == "failed"

        approve_resp = await async_client.post(f"/api/v1/auto-print/{flow_id}/approve")
        assert approve_resp.status_code == 409, approve_resp.text
        assert approve_resp.json()["detail"]

        discard_resp = await async_client.post(f"/api/v1/auto-print/{flow_id}/discard")
        assert discard_resp.status_code == 409, discard_resp.text

    @pytest.mark.asyncio
    async def test_approve_and_discard_unknown_flow_404(self, async_client: AsyncClient):
        assert (await async_client.post("/api/v1/auto-print/999999/approve")).status_code == 404
        assert (await async_client.post("/api/v1/auto-print/999999/discard")).status_code == 404

    @pytest.mark.asyncio
    async def test_concurrent_double_approve_yields_one_queue_item(
        self, flow_session_factory, printer_factory, monkeypatch, mock_sidecar, tmp_path, db_session
    ):
        """Two `approve_flow` calls racing on the same flow id must not both
        queue — this is the same class of bug `_queue_it` was already fixed
        for once (a stage transition observed as "done" before the awaited
        work behind it actually was), here guarded by `_FlowState.
        approval_lock` against a genuine concurrent caller instead of the
        worker's own sequencing."""
        flow_id = await _start_paused_flow(flow_session_factory, printer_factory, monkeypatch, tmp_path)
        paused = await _poll_flow(flow_id, timeout=15.0)
        assert paused.stage == "awaiting_approval", paused.error

        results = await asyncio.gather(
            auto_print_flow.approve_flow(flow_session_factory, flow_id),
            auto_print_flow.approve_flow(flow_session_factory, flow_id),
            return_exceptions=True,
        )

        successes = [r for r in results if isinstance(r, AutoPrintFlow)]
        conflicts = [r for r in results if isinstance(r, auto_print_flow.FlowStageConflictError)]
        assert len(successes) == 1, results
        assert len(conflicts) == 1, results
        assert successes[0].stage == "queued"
        assert successes[0].queue_item_id is not None

        assert await _queue_item_count(db_session) == 1

    @pytest.mark.asyncio
    async def test_require_approval_false_skips_the_pause(
        self, flow_session_factory, printer_factory, monkeypatch, mock_sidecar, tmp_path, db_session
    ):
        """Covered end-to-end already by
        `TestAutoPrintFlowWorker.test_happy_path_reaches_queued`; this test
        just pins the specific claim the approval gate adds risk to — that
        `require_approval=False` never passes through `awaiting_approval` at
        all, it goes straight to `queued`."""
        flow_id = await _start_paused_flow(
            flow_session_factory, printer_factory, monkeypatch, tmp_path, require_approval=False
        )
        flow = await _poll_flow(flow_id, timeout=15.0)

        assert flow.stage == "queued", flow.error
        assert flow.queue_item_id is not None
        assert await _queue_item_count(db_session) == 1


# ---------------------------------------------------------------------------
# Round 2 — brim reaches the SliceRequest
# ---------------------------------------------------------------------------


class TestBrimReachesSliceRequest:
    @pytest.mark.asyncio
    async def test_brim_on_by_default(self, flow_session_factory, printer_factory, monkeypatch, mock_sidecar, tmp_path):
        captured: dict = {}

        async def fake_run_slice(
            flow, *, db_session_factory, model_bytes, model_filename, folder_id, slice_request, owner_id
        ):
            captured["slice_request"] = slice_request
            return {
                "library_file_id": 999999,
                "print_time_seconds": 1775,
                "filament_used_g": 4.58,
                "filament_used_mm": 1500.0,
            }

        monkeypatch.setattr(auto_print_flow, "_run_slice", fake_run_slice)

        flow_id = await _start_paused_flow(flow_session_factory, printer_factory, monkeypatch, tmp_path)
        flow = await _poll_flow(flow_id, timeout=15.0)

        assert flow.stage == "awaiting_approval", flow.error
        slice_request = captured["slice_request"]
        assert slice_request.brim_type == "outer_and_inner"
        assert slice_request.brim_width == 5.0
        assert flow.presets is not None
        assert flow.presets.brim == "Inner + outer, 5 mm"

    @pytest.mark.asyncio
    async def test_brim_off_falls_back_to_the_profile_default(
        self, flow_session_factory, printer_factory, monkeypatch, mock_sidecar, tmp_path
    ):
        """Switching the brim toggle off means "stop forcing an inner+outer
        brim", not "forbid a brim". It must map to the profile's own
        ``auto_brim`` rather than ``no_brim``: the latter would strip the
        adhesion aid from a part the slicer would otherwise have given one to,
        turning an off-toggle into a cause of failed prints."""
        captured: dict = {}

        async def fake_run_slice(
            flow, *, db_session_factory, model_bytes, model_filename, folder_id, slice_request, owner_id
        ):
            captured["slice_request"] = slice_request
            return {"library_file_id": 999999, "print_time_seconds": 1746, "filament_used_g": 4.43}

        monkeypatch.setattr(auto_print_flow, "_run_slice", fake_run_slice)

        flow_id = await _start_paused_flow(flow_session_factory, printer_factory, monkeypatch, tmp_path, brim=False)
        flow = await _poll_flow(flow_id, timeout=15.0)

        assert flow.stage == "awaiting_approval", flow.error
        slice_request = captured["slice_request"]
        assert slice_request.brim_type == "auto_brim"
        assert slice_request.brim_width is None
        assert flow.presets is not None
        assert flow.presets.brim == "Automatic"

    @pytest.mark.asyncio
    async def test_custom_brim_width_reaches_slice_request(
        self, flow_session_factory, printer_factory, monkeypatch, mock_sidecar, tmp_path
    ):
        captured: dict = {}

        async def fake_run_slice(
            flow, *, db_session_factory, model_bytes, model_filename, folder_id, slice_request, owner_id
        ):
            captured["slice_request"] = slice_request
            return {"library_file_id": 999999, "print_time_seconds": 1746, "filament_used_g": 4.43}

        monkeypatch.setattr(auto_print_flow, "_run_slice", fake_run_slice)

        flow_id = await _start_paused_flow(
            flow_session_factory, printer_factory, monkeypatch, tmp_path, brim=True, brim_width=2.5
        )
        flow = await _poll_flow(flow_id, timeout=15.0)

        assert flow.stage == "awaiting_approval", flow.error
        slice_request = captured["slice_request"]
        assert slice_request.brim_type == "outer_and_inner"
        assert slice_request.brim_width == 2.5
        assert flow.presets is not None
        assert flow.presets.brim == "Inner + outer, 2.5 mm"
