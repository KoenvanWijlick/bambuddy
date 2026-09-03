"""Dev-only harness: make `printer_manager` report a canned IDLE printer with
loaded AMS trays, so the auto-print pipeline can be exercised end-to-end with
no physical printer reachable.

Why this exists: `POST /api/v1/printers/` refuses to create a printer it
cannot reach over MQTT ("printer_connection_failed", 400) — there is no API
path to a hardware-free printer row. Dev/test `Printer` rows for this feature
are therefore inserted directly into the DB, which means `printer_manager`
never has an MQTT client for them at all: `is_connected` and `get_status`
return their honest "never heard of this printer" answers (False / None) for
every printer id, not just ones that dropped a connection. Auto-print's
printer/preset selection (`auto_printer_select.select_printer`, and anything
that reads AMS state for the options endpoint) needs *some* live-looking
status to select against, and this module supplies it — without which the
whole pipeline is untestable short of owning real Bambu hardware.

Gating: strictly `BAMBUDDY_MOCK_PRINTER_STATE=1`, **and never under pytest**.
Callers are expected to conditionally import this module (see `auto_print.py`'s
route module) so a production process — where the env var is never set —
doesn't even load it; `install_mock_printer_state()` re-checks the env var
itself regardless, so a stray unconditional import elsewhere still can't turn
the mock on by accident.

The pytest exclusion is not belt-and-braces, it is load-bearing: the patch
targets the shared `printer_manager` singleton and answers for every printer
id, so with the var exported it broke 5 tests in
`backend/tests/unit/test_scheduled_drying_routes.py` (including
`test_offline_printer_is_still_schedulable`, whose whole point is that a
printer is *not* reachable). Those tests pass with the var unset. Since a dev
doing manual UI testing will have the var exported in their shell, the suite
has to be immune to it rather than relying on them remembering to unset it.

Everything mock-related lives in this one module by design (per the spec:
"this is a test harness, not a product feature: keep it in one module and do
not thread mock branches through production code"). It works by monkey-
patching a handful of methods directly onto the shared `printer_manager`
singleton instance — never by editing `PrinterManager` itself — so the real
class stays entirely free of `if MOCK:` branches. The patch is idempotent:
calling `install_mock_printer_state()` more than once (e.g. because more than
one auto-print module imports this at load time) is a no-op after the first
call.
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

_MOCK_ENV_VAR = "BAMBUDDY_MOCK_PRINTER_STATE"

# One AMS unit, 4 slots: PETG in the exact blue the spec's own examples use
# (#0A6CF5), two PLA colours, and one empty slot (no tray_type) — a realistic
# "partially loaded AMS" rather than a suspiciously-full one. `tray_color` is
# 8-hex RRGGBBAA (Bambu always reports an alpha byte); `tray_id_name` is the
# human colour name Bambu Studio shows in its own UI (see
# `spool_tag_matcher.py`'s comment on the same field).
_MOCK_RAW_DATA = {
    "ams": [
        {
            "id": 0,
            "tray": [
                {"id": 0, "tray_type": "PETG", "tray_color": "0A6CF5FF", "tray_id_name": "Blue"},
                {"id": 1, "tray_type": "PLA", "tray_color": "1FB25AFF", "tray_id_name": "Bambu Green"},
                {"id": 2, "tray_type": "PLA", "tray_color": "1A1A1AFF", "tray_id_name": "Black"},
                {"id": 3, "tray_type": "", "tray_color": "", "tray_id_name": ""},
            ],
        }
    ],
    "vt_tray": [],
}


def _build_mock_state():
    """A fresh `PrinterState` per call — callers may mutate the returned
    object's `raw_data` in place (existing code does, e.g. `_remember_trays`),
    so handing out a shared singleton would let one caller's mutation leak
    into another's read.
    """
    from backend.app.services.bambu_mqtt import PrinterState

    return PrinterState(
        connected=True,
        state="IDLE",
        raw_data={
            "ams": [dict(unit, tray=[dict(t) for t in unit["tray"]]) for unit in _MOCK_RAW_DATA["ams"]],
            "vt_tray": list(_MOCK_RAW_DATA["vt_tray"]),
        },
    )


def install_mock_printer_state(*, allow_under_pytest: bool = False) -> None:
    """Monkey-patch `printer_manager` to report the canned IDLE + AMS state
    for ANY printer id, connected or not. No-op unless
    `BAMBUDDY_MOCK_PRINTER_STATE=1`, and idempotent once installed.

    `allow_under_pytest` exists solely for this module's OWN tests, which have
    to actually install the patch to assert on it. Nothing in the application
    passes it, so a normal test run can never get the mock installed behind its
    back — see the module docstring on why that matters.
    """
    if os.environ.get(_MOCK_ENV_VAR) != "1":
        return

    # Never install under pytest, even with the env var set. The patch lands on
    # the shared `printer_manager` singleton and reports IDLE + loaded AMS for
    # *every* printer id, which silently rewrites the premise of any test that
    # asserts on real printer state — `test_scheduled_drying_routes.py`'s
    # `test_offline_printer_is_still_schedulable` is exactly that, and it fails
    # for a mock-shaped reason rather than a real one. A developer running the
    # suite on a machine where they'd exported the var for manual UI testing
    # would otherwise get 5 confusing failures in an unrelated module.
    if not allow_under_pytest and ("PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules):
        logger.debug("%s=1 ignored: running under pytest.", _MOCK_ENV_VAR)
        return

    from backend.app.services.printer_manager import printer_manager

    if getattr(printer_manager, "_auto_print_mock_installed", False):
        return

    logger.warning(
        "%s=1: printer_manager is reporting a MOCK idle printer + AMS trays for every "
        "printer id. Never enable this in production.",
        _MOCK_ENV_VAR,
    )

    # `is_connected` / `get_status` are the two calls every printer-selection
    # and dispatch-eligibility path in the codebase goes through
    # (`print_scheduler._is_printer_idle`, `_count_override_color_matches`,
    # this feature's own `auto_print_options._tray_reading`, and
    # `auto_printer_select.select_printer`). Patching just these two — rather
    # than also `get_all_statuses` / `get_client` / connect/disconnect — is
    # deliberately narrow: those two are the entire surface auto-print's
    # selection logic needs, and leaving the rest alone means a real
    # printer's actual connect/disconnect lifecycle (and any code that walks
    # `_clients` directly) is completely unaffected by this harness.
    printer_manager.is_connected = lambda printer_id: True  # noqa: ARG005
    printer_manager.get_status = lambda printer_id: _build_mock_state()  # noqa: ARG005
    # `last_known_trays` is the fallback path for a printer with no live
    # client at all (see its own docstring) — which, for these DB-only rows,
    # is every call. Mocked too so any reader that goes straight to it
    # (instead of `get_status().raw_data`) still sees loaded trays.
    printer_manager.last_known_trays = lambda printer_id: _build_mock_state().raw_data  # noqa: ARG005

    printer_manager._auto_print_mock_installed = True
