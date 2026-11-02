"""Phase 11 — verify the market_research action is discoverable by
`core.action_loader` without any main.py edits.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_market_research_action_module_imports() -> None:
    mod = importlib.import_module("actions.market_research")
    assert hasattr(mod, "TOOL")
    tool = mod.TOOL
    assert tool["name"] == "market_research"
    assert "asset" in tool["parameters"]["properties"]
    assert callable(tool["handler"])


def test_action_loader_discovers_market_research() -> None:
    al = importlib.import_module("core.action_loader")
    actions_dir = ROOT / "actions"
    reg = al.discover_actions(actions_dir=actions_dir, reserved_names=set(), logger=lambda _m: None)
    assert "market_research" in reg.names()


def test_action_declaration_has_description() -> None:
    al = importlib.import_module("core.action_loader")
    reg = al.discover_actions(actions_dir=ROOT / "actions", reserved_names=set(), logger=lambda _m: None)
    decls = reg.get_tool_declarations()
    found = next((d for d in decls if d["name"] == "market_research"), None)
    assert found is not None
    assert found["description"]
