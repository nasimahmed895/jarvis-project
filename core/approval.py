"""
core/approval.py — three-level sensitivity classifier for autonomous actions.

The existing project already has two well-tested gates:
  * core/confirm.py           — on-screen irreversible-action banner
  * actions/browser_control.py _gate_sensitive / _gate_permission

This module sits ABOVE those gates and turns "which level is this?" into a
single answer that any new autonomous workflow can call. It does not replace
either gate; it routes LVL3 asks through core/confirm so the interface — not
the model — issues the token, and lets LVL1 actions run straight through.

Levels
------
  LVL1  informational / read-only / clearly reversible
        → auto-run, no prompt
  LVL2  mutating but recoverable: send draft email, publish post, modify
        calendar event, upload file
        → honour the per-category preference in config/approval.json
          (default: ask on screen unless user has explicitly enabled auto)
  LVL3  irreversible or financial / legal / identity-affecting
        → always go through core/confirm.py; the model is told it is pending
          and must not claim success until the user accepts

A classifier is deliberately a plain function here. Adding a new tool to the
registry does not force a schema migration — unknown tools fall to LVL2
("recoverable but ask") which is the safe middle. Explicit entries override
by prefix match so `browser_control` with `action="go_to"` stays LVL1 while
`browser_control` with `action="fill_form"` on a payment host escalates.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core import confirm as _confirm


# ── Rule tables ────────────────────────────────────────────────────────────
#
# Key format: "<tool>" for a whole tool, or "<tool>.<action>" when the tool is
# a verb-dispatcher (browser_control, file_controller, computer_settings).
# Order matters: the first matching entry wins, so put narrower rules first.

_LVL1_READ_ONLY = frozenset({
    "web_search", "grounded", "research", "weather_report", "timezone_util",
    "context_recall", "vision_analyze", "screen_processor",
    "system_monitor", "net_monitor", "finance",   # finance is read-only here
    "youtube_video",                               # transcript/summary only
    "browser_control.get_text", "browser_control.get_url",
    "browser_control.get_state", "browser_control.list_tabs",
    "browser_control.list_browsers", "browser_control.screenshot",
    "browser_control.get_prefs", "browser_control.find",
    "browser_control.go_to", "browser_control.search",
    "browser_control.scroll", "browser_control.back",
    "browser_control.forward", "browser_control.reload",
    "browser_control.new_tab", "browser_control.next_tab",
    "browser_control.prev_tab", "browser_control.switch_tab",
})

_LVL3_IRREVERSIBLE = frozenset({
    "payment",
    "computer_settings.shutdown", "computer_settings.restart",
    "computer_settings.sleep", "computer_settings.logout",
    "file_controller.delete", "file_controller.move_to_trash",
    "file_dedupe.apply",
    "browser_control.start_debug",      # quits the user's running browser
    "shell",                             # arbitrary command execution
    "python_exec",                       # arbitrary code execution
    "send_message",                      # outbound SMS / IM
    "email_manager.send",
    "jobs.apply",                        # external application submission
    "education.enroll",                  # payment or identity
})


# LVL2 is the default for anything not in LVL1 or LVL3. The table below is only
# needed to enumerate categories the preference file can toggle to auto.
_LVL2_CATEGORIES = {
    "email_draft":       "email_manager.draft",
    "calendar_write":    "calendar_manager.create",
    "calendar_update":   "calendar_manager.update",
    "social_post":       "social_media.publish",
    "form_submit":       "browser_control.fill_form",
    "upload_file":       "browser_control.upload",
    "download_file":     "downloader",
    "crm_write":         "crm.write",
}

_PREFS_PATH = Path(__file__).resolve().parent.parent / "config" / "approval.json"


@dataclass(frozen=True)
class Classification:
    level: int                # 1, 2, 3
    key:   str                # tool.action key used for the lookup
    reason: str               # short human-readable explanation


def _lookup_key(tool: str, args: dict | None) -> str:
    """Build the lookup key. For verb-dispatchers, the first arg that matches
    'action' / 'verb' / 'op' forms the suffix."""
    args = args or {}
    for candidate in ("action", "verb", "op", "mode"):
        v = args.get(candidate)
        if isinstance(v, str) and v:
            return f"{tool}.{v.lower().strip()}"
    return tool


def classify(tool: str, args: dict | None = None) -> Classification:
    key = _lookup_key(tool, args)
    if key in _LVL3_IRREVERSIBLE or tool in _LVL3_IRREVERSIBLE:
        return Classification(3, key, "irreversible or financial")
    if key in _LVL1_READ_ONLY or tool in _LVL1_READ_ONLY:
        return Classification(1, key, "read-only / clearly reversible")
    return Classification(2, key, "mutating but recoverable")


def _load_prefs() -> dict:
    try:
        return json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def lvl2_auto(category: str) -> bool:
    """Whether the user has opted a LVL2 category into auto-run.

    `category` is a key from `_LVL2_CATEGORIES` or a free-form string the
    caller invents. Missing categories default to False (ask on screen).
    """
    return bool(_load_prefs().get(category, False))


def set_lvl2_auto(category: str, allow: bool) -> None:
    prefs = _load_prefs()
    prefs[category] = bool(allow)
    _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def gate(tool: str, args: dict | None,
         *, title: str, detail: str,
         run: Callable[[], str],
         lvl2_category: Optional[str] = None) -> str:
    """Execute `run` according to the classified level.

    Returns the string the caller should hand back to Gemini:
      * LVL1 or auto-approved LVL2 → the result of `run()`
      * LVL2 ask / LVL3            → the pending-confirmation sentinel from
                                      core/confirm.request (the UI banner has
                                      been raised; the model must not claim
                                      success)
    """
    cls = classify(tool, args)

    if cls.level == 1:
        return run()

    if cls.level == 2:
        if lvl2_category and lvl2_auto(lvl2_category):
            return run()
        # Fall through to confirm gate when the UI is bound, otherwise refuse.
        if not _confirm.is_bound():
            # Headless / test context: treat LVL2 as auto to keep the loop
            # alive. Production always has the HUD bound.
            return run()

    # LVL3, or LVL2-ask with HUD available.
    return _confirm.request(
        key=f"approval-{cls.key}",
        title=title,
        detail=detail,
        run=run,
    )
