"""
macOS AppleScript bridge for Chromium-family browsers.

Why this exists:
    Chrome refuses to expose the CDP debug port after launch — the
    --remote-debugging-port flag is only honoured at startup. Requiring the
    user to quit and reopen Chrome every time they want JARVIS to touch their
    actual tabs is a terrible UX, and until they do, Playwright falls back to
    a separate automation profile that is NOT logged into anything.

    macOS has an escape hatch: the Chromium browsers all ship an AppleScript
    dictionary ("GoogleChromeSuite" / "BraveSuite" / etc.) that lets us read
    the active tab's URL and title, run arbitrary JavaScript in it, enumerate
    windows and tabs, and switch between them. No relaunch, no new profile,
    no CDP port. Enough to answer "which account am I logged in as?" and
    "scroll THIS page" against the user's real browser.

Limits:
    - macOS only. Non-Darwin callers get a clear error up front.
    - First call per browser pops a one-time macOS Automation permission
      dialog. If the user denies, osascript returns -1743 and callers can
      fall back cleanly.
    - A locked keychain or Private window may refuse `execute javascript`.
      That one is a Chrome user preference (View → Developer → Allow
      JavaScript from Apple Events), not something we can turn on remotely.
"""
from __future__ import annotations

import json
import platform
import shlex
import subprocess
from typing import Any

_SUPPORTED = {
    "chrome":   "Google Chrome",
    "brave":    "Brave Browser",
    "edge":     "Microsoft Edge",
    "vivaldi":  "Vivaldi",
    "opera":    "Opera",
    "operagx":  "Opera GX",
}


class AppleScriptError(RuntimeError):
    pass


def available() -> bool:
    return platform.system() == "Darwin"


def app_name(browser: str) -> str | None:
    return _SUPPORTED.get((browser or "").lower().strip())


def _run_osascript(script: str, timeout: float = 10.0) -> str:
    """Run an AppleScript source string. Raises AppleScriptError on failure
    with the exact osascript stderr — callers key on substrings like
    '-1743' (Automation denied) to decide whether to fall back."""
    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise AppleScriptError("osascript not found — macOS only.")
    except subprocess.TimeoutExpired:
        raise AppleScriptError("osascript timed out.")
    if res.returncode != 0:
        raise AppleScriptError((res.stderr or res.stdout).strip())
    return res.stdout.rstrip("\n")


def _escape_js_for_applescript(js: str) -> str:
    """AppleScript strings use " as delimiter and \\ as escape. Keep JS
    source intact across the shell → osascript → AppleScript boundary."""
    return js.replace("\\", "\\\\").replace('"', '\\"')


def is_running(browser: str) -> bool:
    """True if the macOS app is currently running (process exists)."""
    app = app_name(browser)
    if not app:
        return False
    # `pgrep -x` returns 0 if a process with that exact name exists. The
    # Chromium main process name on macOS matches the .app bundle name.
    try:
        res = subprocess.run(
            ["pgrep", "-x", app],
            capture_output=True, text=True, timeout=3,
        )
        return res.returncode == 0
    except Exception:
        return False


# ── Reads ────────────────────────────────────────────────────────────────────

def active_url(browser: str) -> str:
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    return _run_osascript(
        f'tell application "{app}" to return URL of active tab of front window'
    )


def active_title(browser: str) -> str:
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    return _run_osascript(
        f'tell application "{app}" to return title of active tab of front window'
    )


def execute_js(browser: str, js: str) -> str:
    """Run JavaScript in the active tab and return its last expression as a
    string. Chrome preference 'Allow JavaScript from Apple Events' must be
    enabled — if it isn't, the error surfaces unchanged to the caller."""
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    esc = _escape_js_for_applescript(js)
    script = (
        f'tell application "{app}"\n'
        f'  tell active tab of front window\n'
        f'    set theResult to execute javascript "{esc}"\n'
        f'  end tell\n'
        f'  return theResult as text\n'
        f'end tell'
    )
    return _run_osascript(script)


def get_text(browser: str, limit: int = 4000) -> str:
    """Returns the visible text of the active tab, truncated."""
    txt = execute_js(browser, "document.body.innerText")
    return txt[:limit]


def scroll(browser: str, direction: str = "down", amount: int = 600) -> str:
    y = amount if direction == "down" else -amount
    execute_js(browser, f"window.scrollBy(0, {int(y)});")
    return f"Scrolled {direction} by {abs(int(y))}px in {browser}."


def click_text(browser: str, text: str) -> str:
    """Best-effort click: finds the first element whose innerText contains
    the given text (case-insensitive) and clicks it. For precise clicks the
    CDP path is still better — but a lot of 'click the login button' asks
    land here just fine."""
    safe = text.replace("'", "\\'").replace('"', '\\"')
    js = (
        "(() => {"
        f"const t = '{safe}'.toLowerCase();"
        "const els = Array.from(document.querySelectorAll('a,button,[role=\"button\"],input[type=\"submit\"]'));"
        "const hit = els.find(e => (e.innerText||e.value||'').toLowerCase().includes(t) && e.offsetParent !== null);"
        "if (!hit) return 'NOT_FOUND';"
        "hit.click(); return 'OK';"
        "})()"
    )
    out = execute_js(browser, js)
    return f"Clicked: {text}" if out.strip() == "OK" else f"Could not find clickable element matching: {text}"


# ── Tab listing / switching ───────────────────────────────────────────────────

def list_tabs(browser: str) -> list[dict]:
    """Returns [{window, tab, title, url, active}, ...] across every window."""
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    # AppleScript records aren't JSON. Easiest path: build a `|`-separated
    # table via text concat, parse in Python. Titles may contain `|`, so we
    # URL-encode them lightly with %7C before joining.
    script = (
        f'set outLines to {{}}\n'
        f'tell application "{app}"\n'
        f'  set winIdx to 0\n'
        f'  repeat with w in windows\n'
        f'    set winIdx to winIdx + 1\n'
        f'    set tabIdx to 0\n'
        f'    set actId to id of active tab of w\n'
        f'    repeat with t in tabs of w\n'
        f'      set tabIdx to tabIdx + 1\n'
        f'      set theTitle to my encBar(title of t)\n'
        f'      set theURL to URL of t\n'
        f'      set isActive to ((id of t) is actId)\n'
        f'      set end of outLines to ((winIdx as text) & "|" & (tabIdx as text) & "|" & theTitle & "|" & theURL & "|" & (isActive as text))\n'
        f'    end repeat\n'
        f'  end repeat\n'
        f'end tell\n'
        f'set AppleScript\'s text item delimiters to linefeed\n'
        f'set out to outLines as text\n'
        f'set AppleScript\'s text item delimiters to ""\n'
        f'return out\n'
        f'on encBar(s)\n'
        f'  set AppleScript\'s text item delimiters to "|"\n'
        f'  set parts to text items of s\n'
        f'  set AppleScript\'s text item delimiters to "%7C"\n'
        f'  set out to parts as text\n'
        f'  set AppleScript\'s text item delimiters to ""\n'
        f'  return out\n'
        f'end encBar'
    )
    raw = _run_osascript(script, timeout=12.0)
    tabs: list[dict] = []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        tabs.append({
            "window": int(parts[0]),
            "tab":    int(parts[1]),
            "title":  parts[2].replace("%7C", "|"),
            "url":    parts[3],
            "active": parts[4].strip().lower() == "true",
        })
    return tabs


def switch_tab(browser: str,
                index: int | None = None,
                title_contains: str | None = None) -> str:
    """Activate the matching tab (and bring its window to the front).
    `index` is a flat index across all windows, matching list_tabs() order."""
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    tabs = list_tabs(browser)
    if not tabs:
        return "No open tabs."
    target = None
    if index is not None:
        if 0 <= index < len(tabs):
            target = tabs[index]
        else:
            return f"Tab index {index} out of range (0..{len(tabs)-1})."
    elif title_contains:
        needle = title_contains.lower()
        for t in tabs:
            if needle in t["title"].lower() or needle in t["url"].lower():
                target = t
                break
        if not target:
            return f"No tab matches: '{title_contains}'"
    else:
        return "Specify index or title_contains."
    script = (
        f'tell application "{app}"\n'
        f'  set active tab index of window {target["window"]} to {target["tab"]}\n'
        f'  set index of window {target["window"]} to 1\n'
        f'  activate\n'
        f'end tell'
    )
    _run_osascript(script)
    return f"Switched to tab: {target['title'][:60]}"
