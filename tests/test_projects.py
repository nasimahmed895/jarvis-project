"""Tests for core/projects.py and actions/project.py."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import projects as proj
import actions.project as proj_action


def _fresh():
    proj.connect(Path(tempfile.mktemp(suffix="-proj.sqlite")))
    proj_action._CONNECTED = True


def test_add_and_get_project():
    _fresh()
    pid = proj.add_project("Launch", description="Q1 campaign",
                           status="active", target_date="2099-03-31")
    p = proj.get_project(pid)
    assert p and p["name"] == "Launch" and p["status"] == "active"


def test_add_rejects_empty_name():
    _fresh()
    try:
        proj.add_project("")
    except ValueError:
        return
    raise AssertionError("expected ValueError on empty name")


def test_status_transitions_record_actual_end():
    _fresh()
    pid = proj.add_project("X", status="active")
    assert proj.set_project_status(pid, "done")
    p = proj.get_project(pid)
    assert p["status"] == "done"
    assert p["actual_end"]


def test_status_normalisation():
    _fresh()
    pid = proj.add_project("Y", status="WEIRD")  # falls back
    assert proj.get_project(pid)["status"] == "planning"


def test_milestone_lifecycle():
    _fresh()
    pid = proj.add_project("M")
    mid = proj.add_milestone(pid, "Alpha", target_date="2099-01-15")
    assert proj.set_milestone_status(mid, "done")
    rows = proj.list_milestones(pid)
    assert rows and rows[0]["status"] == "done"


def test_project_tasks_crud_and_filter():
    _fresh()
    pid = proj.add_project("P")
    t1 = proj.add_project_task(pid, "do thing")
    t2 = proj.add_project_task(pid, "do other", due_date="2099-05-01")
    assert proj.set_project_task_status(t1, "done")
    open_tasks = proj.list_project_tasks(pid, status="open")
    assert len(open_tasks) == 1 and open_tasks[0]["id"] == t2
    done_tasks = proj.list_project_tasks(pid, status="done")
    assert len(done_tasks) == 1 and done_tasks[0]["id"] == t1


def test_project_summary_completion_pct():
    _fresh()
    pid = proj.add_project("S")
    for _ in range(3):
        proj.add_project_task(pid, "a")
    t4 = proj.add_project_task(pid, "b")
    proj.set_project_task_status(t4, "done")
    s = proj.project_summary(pid)
    assert s["total_tasks"] == 4
    assert s["completion_percent"] == 25.0


def test_project_summary_picks_next_pending_milestone():
    _fresh()
    pid = proj.add_project("N")
    old = proj.add_milestone(pid, "old", target_date="1999-01-01")
    nxt = proj.add_milestone(pid, "next", target_date="2099-01-01")
    proj.set_milestone_status(old, "done")
    s = proj.project_summary(pid)
    assert s["next_milestone"]["id"] == nxt


def test_search_matches_description():
    _fresh()
    proj.add_project("Alpha", description="the kickoff")
    proj.add_project("Beta", description="unrelated thing")
    rows = proj.search_projects("kickoff")
    assert len(rows) == 1 and rows[0]["name"] == "Alpha"


def test_find_project_case_insensitive():
    _fresh()
    proj.add_project("Zephyr")
    hit = proj.find_project("zEpHyR")
    assert hit and hit["name"] == "Zephyr"


def test_list_filters_by_status_list():
    _fresh()
    a = proj.add_project("a", status="active")
    b = proj.add_project("b", status="on_hold")
    c = proj.add_project("c", status="done")
    rows = proj.list_projects(status=["active", "on_hold"])
    ids = {r["id"] for r in rows}
    assert ids == {a, b}


# ── Action layer ─────────────────────────────────────────────────────────────

def test_action_add_and_list():
    _fresh()
    out = proj_action.project({"action": "add", "name": "campaign"})
    assert "Created project" in out
    out = proj_action.project({"action": "list"})
    assert "campaign" in out


def test_action_add_milestone_requires_project_id():
    _fresh()
    assert "project_id" in proj_action.project({
        "action": "add_milestone", "name": "x",
    })


def test_action_summary_returns_counts():
    _fresh()
    pid = proj.add_project("S")
    t = proj.add_project_task(pid, "hello")
    proj.set_project_task_status(t, "done")
    out = proj_action.project({"action": "summary", "id": pid})
    assert "Completion: 100.0%" in out


def test_action_search():
    _fresh()
    proj.add_project("MLSeries")
    out = proj_action.project({"action": "search", "query": "mls"})
    assert "MLSeries" in out


def test_action_unknown():
    _fresh()
    assert "unknown action" in proj_action.project({"action": "nope"})


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    sys.exit(1 if failed else 0)
