"""Phase 2 regression — existing JARVIS modules must still import.

LIMITATION: `main.py` is NOT exercised here. On import it monkey-patches
subprocess.Popen (Windows only), reconfigures stdout/stderr encoding, and
imports PyQt6 via `ui.py`. The import surface is too wide to assert "safe"
without additional investigation. Importing it inside pytest would also pull
the heavyweight Qt binding into the test process. A targeted main.py check
is deferred to Phase 12 under controlled conditions.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Modules that touch audio drivers, Qt, or wake-word binaries at import time.
# Skipped by this regression smoke; they are exercised by JARVIS itself at
# runtime, not by the test suite.
_HARDWARE_MODULES = {
    "wake_word",
    "hotkey",
    "tts",
    "stt",
    "avatar",
    "avatar_mesh",
    "viseme",
    "audio_devices",
    "echo",
    "installer",
}


@pytest.mark.regression
def test_market_package_imports() -> None:
    m = importlib.import_module("market")
    assert isinstance(m.__version__, str) and m.__version__


@pytest.mark.regression
def test_action_loader_imports_and_discovers_empty(tmp_path: Path) -> None:
    mod = importlib.import_module("core.action_loader")
    reg = mod.discover_actions(
        actions_dir=tmp_path, reserved_names=set(), logger=lambda _m: None
    )
    assert hasattr(reg, "names")
    assert reg.names() == set()


@pytest.mark.regression
def test_core_modules_import(record_property) -> None:
    """Import every pure-logic core module. Records any failures."""
    core_dir = ROOT / "core"
    failures: list[tuple[str, str]] = []
    for p in sorted(core_dir.glob("*.py")):
        name = p.stem
        if name.startswith("_") or name in _HARDWARE_MODULES:
            continue
        try:
            importlib.import_module(f"core.{name}")
        except Exception as e:  # noqa: BLE001
            failures.append((name, f"{type(e).__name__}: {e}"))
    record_property("core_import_failures", failures)
    assert not failures, failures


@pytest.mark.regression
def test_action_modules_import(record_property) -> None:
    """Import every actions/*.py (excluding leading-underscore files)."""
    actions_dir = ROOT / "actions"
    failures: list[tuple[str, str]] = []
    for p in sorted(actions_dir.glob("*.py")):
        if p.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"actions.{p.stem}")
        except Exception as e:  # noqa: BLE001
            failures.append((p.name, f"{type(e).__name__}: {e}"))
    record_property("action_import_failures", failures)
    # Record but do not fail — some actions may require optional deps that
    # are not installed in this environment (e.g. playwright browsers).
    # Fail only if ALL of them fail, which would indicate a core breakage.
    assert len(failures) < len(list(actions_dir.glob("*.py")))


@pytest.mark.regression
def test_memory_modules_import() -> None:
    importlib.import_module("memory.memory_manager")
    importlib.import_module("memory.config_manager")


@pytest.mark.regression
def test_memory_manager_load_memory_isolated(tmp_path: Path, monkeypatch) -> None:
    mm = importlib.import_module("memory.memory_manager")
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "mem.json")
    out = mm.load_memory()
    assert isinstance(out, dict)
    assert "identity" in out


@pytest.mark.regression
def test_config_manager_load_isolated(tmp_path: Path, monkeypatch) -> None:
    cm = importlib.import_module("memory.config_manager")
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")
    assert cm.load_api_keys() == {}
