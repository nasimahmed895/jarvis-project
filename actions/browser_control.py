
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import platform
import shutil
import subprocess
import threading
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import urllib.request

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
)

try:
    from core import confirm as _confirm
except Exception:
    _confirm = None  # core.confirm optional; permission/sensitive gates degrade gracefully

try:
    from actions import _mac_chrome as _macchrome
except Exception:
    _macchrome = None  # optional — only used on macOS for Chromium browsers


# Trust mode is a per-user "this is MY assistant on MY machine" opt-in. When
# the config flag is set, "may I access your browser?" and "may I watch your
# camera?" style gates auto-grant. Genuinely destructive actions (quitting
# the user's running Chrome, submitting a form with their data, powering the
# machine off) KEEP asking — trust mode is a convenience switch, not a way to
# surrender those.
_TRUST_CFG_KEY = "jarvis_trust_me"


def _trust_me() -> bool:
    try:
        from actions.screen_processor import _load_config
        return bool(_load_config().get(_TRUST_CFG_KEY, False))
    except Exception:
        return False


def _set_trust(flag: bool) -> None:
    from actions.screen_processor import _save_config_key
    _save_config_key(_TRUST_CFG_KEY, bool(flag))


# ── Scoped per-site trust ────────────────────────────────────────────────────
# A binary `trust_me` is a heavy hammer. Users sometimes want to say "it's
# fine to fill forms on rootdevs.com and my-company.com, but keep asking
# elsewhere". These helpers maintain a small allowlist of hostnames that
# skip the confirmation gate.
_SCOPED_TRUST_KEY = "browser_scoped_trust"


def _scoped_trust_hosts() -> set[str]:
    try:
        from actions.screen_processor import _load_config
        raw = _load_config().get(_SCOPED_TRUST_KEY, []) or []
        return {str(h).lower().strip() for h in raw if h}
    except Exception:
        return set()


def _save_scoped_trust(hosts: set[str]) -> None:
    try:
        from actions.screen_processor import _save_config_key
        _save_config_key(_SCOPED_TRUST_KEY, sorted(hosts))
    except Exception:
        pass


def _host_from_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_scoped_trusted() -> bool:
    """True when the current browser's active URL host is on the allowlist."""
    try:
        from actions import _mac_chrome as _mc
        if not _mc.available() or not _mc.is_running("chrome"):
            return False
        host = _host_from_url(_mc.active_url("chrome"))
    except Exception:
        return False
    if not host:
        return False
    hosts = _scoped_trust_hosts()
    if host in hosts:
        return True
    # Match "*.rootdevs.com" patterns too
    for h in hosts:
        if h.startswith("*.") and host.endswith(h[1:]):
            return True
    return False


# ── User preferences for browser automation ─────────────────────────────────
# Common form values (name, email, phone, company, website, address) saved
# once and auto-filled whenever a form has matching fields. Lives next to the
# other config under config/api_keys.json so `trust_me` and prefs share one
# home.
_PREFS_KEY = "browser_prefs"


def _load_prefs() -> dict:
    try:
        from actions.screen_processor import _load_config
        return dict(_load_config().get(_PREFS_KEY, {}) or {})
    except Exception:
        return {}


def _save_prefs(prefs: dict) -> None:
    try:
        from actions.screen_processor import _save_config_key
        _save_config_key(_PREFS_KEY, dict(prefs or {}))
    except Exception:
        pass


def _merge_prefs_into_fields(fields: dict) -> dict:
    """Autocomplete a fill_form request with whatever the user has saved.
    Explicit values in `fields` always win."""
    out = dict(fields or {})
    prefs = _load_prefs()
    for k, v in prefs.items():
        # Only fill keys the model didn't supply, and only non-empty prefs.
        if k not in out and v:
            out[k] = v
    return out


def _js_str(s: str) -> str:
    """Safely serialise a Python string into a JS string literal."""
    import json as _j
    return _j.dumps(str(s))


_REACT_RECT_JS = ""  # placeholder — rect computation is inline, kept for future


def _os_pointer(x: int, y: int, click: bool) -> str:
    """Move the OS pointer to (x,y) in screen space and optionally left-click.
    Uses `cliclick` when it's on PATH (precise, pass-through), otherwise falls
    back to AppleScript System Events (which can click but not move the real
    cursor on modern macOS)."""
    cli = shutil.which("cliclick")
    if cli:
        if click:
            cmd = [cli, f"c:{x},{y}"]
        else:
            cmd = [cli, f"m:{x},{y}"]
        try:
            subprocess.run(cmd, check=True, timeout=5)
            return f"{'Clicked' if click else 'Hovered'} at ({x},{y})."
        except subprocess.CalledProcessError as e:
            return f"cliclick failed: {e}"
    # No cliclick: AppleScript System Events can synthesise a click via GUI
    # scripting. It doesn't visibly move the cursor, but the event lands.
    if not click:
        return (f"Pointer move to ({x},{y}) needs `cliclick` installed. "
                f"Run `brew install cliclick`.")
    script = (
        f'tell application "System Events"\n'
        f'  click at {{{x}, {y}}}\n'
        f'end tell'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True, timeout=5, capture_output=True, text=True,
        )
        return f"Clicked at ({x},{y})."
    except subprocess.CalledProcessError as e:
        return f"Click at ({x},{y}) failed: {(e.stderr or '')[:120]}"
_OS = platform.system()   # "Windows" | "Darwin" | "Linux"

def _normalize_url(url: str) -> str:
    """
    Bare words like "instagram" → "https://instagram.com"
    Domains like "instagram.com" → "https://instagram.com"
    Full URLs pass through unchanged.
    """
    url = url.strip()
    if not url:
        return "about:blank"
    if "://" in url:
        return url
    # No dot at all → assume .com  (e.g. "instagram" → "instagram.com")
    if "." not in url:
        url = url + ".com"
    return "https://" + url


def _user_agent() -> str:
    if _OS == "Windows":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    if _OS == "Darwin":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


def _real_profile_dir(browser: str) -> str:
    home  = Path.home()
    local = os.environ.get("LOCALAPPDATA", "")
    roam  = os.environ.get("APPDATA", "")

    candidates: list[Path] = []

    if _OS == "Windows":
        m = {
            "chrome":   [Path(local) / "Google"          / "Chrome"          / "User Data"],
            "edge":     [Path(local) / "Microsoft"        / "Edge"            / "User Data"],
            "brave":    [Path(local) / "BraveSoftware"    / "Brave-Browser"   / "User Data"],
            "vivaldi":  [Path(local) / "Vivaldi"          / "User Data"],
            "opera":    [Path(roam)  / "Opera Software"   / "Opera Stable",
                         Path(local) / "Opera Software"   / "Opera Stable"],
            "operagx":  [Path(roam)  / "Opera Software"   / "Opera GX Stable",
                         Path(local) / "Opera Software"   / "Opera GX Stable"],
        }
        candidates = m.get(browser, [])

    elif _OS == "Darwin":
        lib = home / "Library" / "Application Support"
        m = {
            "chrome":   [lib / "Google"             / "Chrome"],
            "edge":     [lib / "Microsoft Edge"],
            "brave":    [lib / "BraveSoftware"       / "Brave-Browser"],
            "vivaldi":  [lib / "Vivaldi"],
            "opera":    [lib / "com.operasoftware.Opera"],
            "operagx":  [lib / "com.operasoftware.OperaGX"],
        }
        candidates = m.get(browser, [])

    elif _OS == "Linux":
        cfg = home / ".config"
        m = {
            "chrome":   [cfg / "google-chrome", cfg / "chromium"],
            "edge":     [cfg / "microsoft-edge"],
            "brave":    [cfg / "BraveSoftware" / "Brave-Browser"],
            "vivaldi":  [cfg / "vivaldi"],
            "opera":    [cfg / "opera"],
            "operagx":  [cfg / "opera-gx"],
        }
        candidates = m.get(browser, [])

    for p in candidates:
        if p.exists():
            print(f"[Browser] ✅ Real profile found for {browser}: {p}")
            return str(p)

    fallback = home / ".jarvis_profiles" / browser
    fallback.mkdir(parents=True, exist_ok=True)
    print(f"[Browser] ⚠️  Real profile not found for {browser}, using: {fallback}")
    return str(fallback)

def _firefox_profile_dir() -> Optional[str]:
    home = Path.home()

    if _OS == "Windows":
        base = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox"
    elif _OS == "Darwin":
        base = home / "Library" / "Application Support" / "Firefox"
    else:
        base = home / ".mozilla" / "firefox"

    ini = base / "profiles.ini"
    if not ini.exists():
        return None

    current: dict[str, str] = {}
    default_path: Optional[str] = None

    for line in ini.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("["):
            p = current.get("Path", "")
            if p and current.get("Default") == "1":
                is_rel = current.get("IsRelative", "1") == "1"
                default_path = str(base / p) if is_rel else p
            current = {}
        elif "=" in line:
            k, _, v = line.partition("=")
            current[k.strip()] = v.strip()

    p = current.get("Path", "")
    if p and current.get("Default") == "1":
        is_rel = current.get("IsRelative", "1") == "1"
        default_path = str(base / p) if is_rel else p

    if default_path and Path(default_path).exists():
        print(f"[Browser] Firefox real profile: {default_path}")
        return default_path
    return None

def _find_opera_windows() -> Optional[str]:
    local  = os.environ.get("LOCALAPPDATA", "")
    prog   = os.environ.get("PROGRAMFILES", "")
    prog86 = os.environ.get("PROGRAMFILES(X86)", "")

    candidates = [
        Path(local)  / "Programs" / "Opera"    / "opera.exe",
        Path(local)  / "Programs" / "Opera GX" / "opera.exe",
        Path(prog)   / "Opera"    / "opera.exe",
        Path(prog86) / "Opera"    / "opera.exe",
    ]
    for p in candidates:
        if p.exists():
            print(f"[Browser] Opera found at: {p}")
            return str(p)

    try:
        import winreg
        keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe",
            r"SOFTWARE\Clients\StartMenuInternet\OperaStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\OperaGXStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\opera\shell\open\command",
        ]
        for key_path in keys:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        print(f"[Browser] Opera found via registry: {exe}")
                        return exe
                except Exception:
                    continue
    except Exception:
        pass

    return shutil.which("opera") or None

def _find_exe_windows(prog_name: str) -> Optional[str]:
    try:
        import winreg
        paths_to_try = [
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{prog_name}.exe",
            rf"SOFTWARE\Clients\StartMenuInternet\{prog_name}\shell\open\command",
        ]
        for key_path in paths_to_try:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        return exe
                except Exception:
                    continue
    except Exception:
        pass
    return None

_BROWSER_SPECS: dict[str, dict] = {
    "Windows": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": []},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox.exe"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera.exe"],  "special": "opera_windows"},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": [],             "special": "opera_windows"},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave.exe"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi.exe"]},
        "safari":   None,
    },
    "Darwin": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": ["microsoft-edge"]},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi"]},
        "safari":   {"engine": "webkit",   "channel": None,      "bins": []},
    },
    "Linux": {
        "chrome":   {"engine": "chromium", "channel": None,
                     "bins": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]},
        "edge":     {"engine": "chromium", "channel": None,
                     "bins": ["microsoft-edge", "microsoft-edge-stable"]},
        "firefox":  {"engine": "firefox",  "channel": None, "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "operagx":  {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "brave":    {"engine": "chromium", "channel": None, "bins": ["brave-browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None, "bins": ["vivaldi-stable", "vivaldi"]},
        "safari":   None,
    },
}

_ALIASES: dict[str, str] = {
    "google chrome":   "chrome",
    "google-chrome":   "chrome",
    "microsoft edge":  "edge",
    "ms edge":         "edge",
    "msedge":          "edge",
    "mozilla firefox": "firefox",
    "opera gx":        "operagx",
    "opera_gx":        "operagx",
}


def _resolve_browser(name: str) -> dict | None:
    name   = _ALIASES.get(name.lower().strip(), name.lower().strip())
    os_map = _BROWSER_SPECS.get(_OS, {})
    spec   = os_map.get(name)
    if spec is None:
        return None

    engine  = spec["engine"]
    channel = spec.get("channel")
    bins    = spec.get("bins", [])
    exe     = None

    if spec.get("special") == "opera_windows":
        exe = _find_opera_windows()
        if not exe:
            print(f"[Browser] ⚠️  Opera executable not found on Windows.")
        return {"engine": engine, "exe": exe, "channel": channel}

    for b in bins:
        found = shutil.which(b)
        if found:
            exe = found
            break

    if not exe and _OS == "Darwin":
        app_names = {
            "chrome":  ["Google Chrome.app"],
            "edge":    ["Microsoft Edge.app"],
            "firefox": ["Firefox.app"],
            "opera":   ["Opera.app", "Opera GX.app"],
            "brave":   ["Brave Browser.app"],
            "vivaldi": ["Vivaldi.app"],
        }
        for app in app_names.get(name, []):
            app_dir = Path("/Applications") / app / "Contents" / "MacOS"
            if app_dir.exists():
                found_bins = list(app_dir.iterdir())
                if found_bins:
                    exe = str(found_bins[0])
                    break

    if not exe and _OS == "Windows" and not channel:
        exe = _find_exe_windows(name)

    return {"engine": engine, "exe": exe, "channel": channel}


def _detect_default_browser() -> str:
    try:
        if _OS == "Windows":
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\http\UserChoice",
            )
            prog_id = winreg.QueryValueEx(k, "ProgId")[0].lower()
            winreg.CloseKey(k)
            for kw in ("edge", "firefox", "opera", "brave", "vivaldi", "chrome"):
                if kw in prog_id:
                    return kw
        elif _OS == "Darwin":
            out = subprocess.run(
                ["defaults", "read",
                 "com.apple.LaunchServices/com.apple.launchservices.secure",
                 "LSHandlers"],
                capture_output=True, text=True, timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "safari", "chrome", "edge"):
                if kw in out:
                    return kw
        elif _OS == "Linux":
            out = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True, timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "chrome", "edge"):
                if kw in out:
                    return kw
    except Exception:
        pass
    return "chrome"


# Default CDP debug ports — user chooses when they relaunch their browser with
# --remote-debugging-port. We only probe; we never set.
_CDP_DEFAULT_PORT = 9222

def _cdp_probe(port: int) -> str | None:
    """Returns the ws endpoint path from http://localhost:<port>/json/version,
    or None if the port is not reachable."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=1.5
        ) as r:
            import json as _json
            data = _json.loads(r.read().decode("utf-8", "replace"))
            return data.get("webSocketDebuggerUrl") or f"http://127.0.0.1:{port}"
    except Exception:
        return None


def _cdp_exe_for(browser: str) -> str | None:
    """OS-specific binary path for the CDP relaunch, or None if unknown."""
    name = _ALIASES.get(browser.lower().strip(), browser.lower().strip())
    table = {
        "Darwin": {
            "chrome":  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "brave":   "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "edge":    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        },
        "Windows": {
            "chrome":  shutil.which("chrome") or _find_exe_windows("chrome"),
            "brave":   shutil.which("brave")  or _find_exe_windows("brave"),
            "edge":    shutil.which("msedge") or _find_exe_windows("msedge"),
        },
        "Linux": {
            "chrome":  shutil.which("google-chrome") or shutil.which("google-chrome-stable"),
            "brave":   shutil.which("brave-browser") or shutil.which("brave"),
            "edge":    shutil.which("microsoft-edge"),
        },
    }
    return table.get(_OS, {}).get(name)


def _quit_running_browser(browser: str) -> None:
    """Terminate every process matching this browser so we can relaunch it
    with the debug port. Needed because Chrome refuses the debug flag when
    another instance is already holding the user profile."""
    try:
        import psutil as _ps
    except Exception:
        return
    name = _ALIASES.get(browser.lower().strip(), browser.lower().strip())
    needles = {
        "chrome":   ("chrome", "chromium"),
        "brave":    ("brave",),
        "edge":     ("msedge", "microsoft edge"),
        "vivaldi":  ("vivaldi",),
        "opera":    ("opera",),
        "operagx":  ("opera gx",),
    }.get(name, (name,))
    victims = []
    for p in _ps.process_iter(attrs=("name", "exe")):
        try:
            info = p.info or {}
            hay  = f"{(info.get('name') or '').lower()} {(info.get('exe') or '').lower()}"
        except Exception:
            continue
        if any(n in hay for n in needles):
            victims.append(p)
    for p in victims:
        try:
            p.terminate()
        except Exception:
            pass
    gone, alive = _ps.wait_procs(victims, timeout=4)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass


def _start_debug_browser(browser: str, port: int = _CDP_DEFAULT_PORT) -> str:
    """Relaunch the user's browser with the CDP debug port exposed, pointing
    at the user's REAL profile so logged-in accounts and extensions survive."""
    name = _ALIASES.get(browser.lower().strip(), browser.lower().strip())
    exe  = _cdp_exe_for(name)
    if not exe:
        return (f"I don't know where {name} is installed on this machine. "
                f"Please launch it manually with --remote-debugging-port={port}.")
    profile = _real_profile_dir(name)
    _quit_running_browser(name)
    try:
        subprocess.Popen(
            [exe, f"--remote-debugging-port={port}",
                  f"--user-data-dir={profile}",
                  "--restore-last-session"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return f"Could not launch {name} in debug mode: {e}"
    # Wait up to 10s for the debug port to come up.
    import time as _t
    for _ in range(40):
        if _cdp_probe(port) is not None:
            # Auto-attach so the next interactive action just works.
            try:
                sess = _registry.get(name, cdp_port=port)
                sess.run(sess.get_url())
            except Exception as e:
                return (f"{name} relaunched in debug mode on :{port}, "
                        f"but attach failed: {e}")
            return (f"Relaunched {name} with debug port :{port} and attached. "
                    f"You're logged in as before — your real profile is in use.")
        _t.sleep(0.25)
    return (f"Relaunched {name}, but port :{port} did not come up within 10s. "
            f"Try again, or launch manually.")


def _cdp_launch_hint(browser: str, port: int) -> str:
    """One-line instruction the user can read aloud and act on."""
    name = _ALIASES.get(browser.lower().strip(), browser.lower().strip())
    exe = {
        "Darwin": {
            "chrome":  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "brave":   "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "edge":    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        },
        "Windows": {
            "chrome":  "chrome.exe",
            "brave":   "brave.exe",
            "edge":    "msedge.exe",
        },
        "Linux": {
            "chrome":  "google-chrome",
            "brave":   "brave-browser",
            "edge":    "microsoft-edge",
        },
    }.get(_OS, {}).get(name, name)
    profile = _real_profile_dir(name)
    return (
        f"To attach, quit {name} first, then relaunch it with: "
        f'"{exe}" --remote-debugging-port={port} --user-data-dir="{profile}"'
    )


# Match a process name/exe path to a known browser slug. Chromium browsers all
# look like "Google Chrome Helper (Renderer)" etc on macOS and like
# "chrome.exe" on Windows — matching on substring of the main exe plus the
# cmdline covers both. Opera renderer processes also run under opera.exe, so
# we uniq by slug.
_BROWSER_SIGS: list[tuple[str, tuple[str, ...]]] = [
    ("operagx",  ("opera gx", "operagx")),
    ("brave",    ("brave",)),
    ("edge",     ("msedge", "microsoft edge")),
    ("vivaldi",  ("vivaldi",)),
    ("opera",    ("opera",)),           # must come after operagx
    ("firefox",  ("firefox",)),
    ("safari",   ("safari",)),
    ("chrome",   ("chrome", "chromium")),
]


def _detect_running_browsers() -> set[str]:
    """Scan running processes for browsers the user has open."""
    try:
        import psutil as _ps
    except Exception:
        return set()
    found: set[str] = set()
    for p in _ps.process_iter(attrs=("name", "exe")):
        try:
            info = p.info or {}
            name = (info.get("name") or "").lower()
            exe  = (info.get("exe")  or "").lower()
        except Exception:
            continue
        hay = f"{name} {exe}"
        for slug, needles in _BROWSER_SIGS:
            if slug in found:
                continue
            if any(n in hay for n in needles):
                # macOS Safari also matches "safari services" etc. Filter out
                # obvious helpers so we don't report "safari" from a system
                # XPC helper when the user doesn't actually have Safari open.
                if "helper" in name or "service" in name:
                    continue
                found.add(slug)
                break
    return found


_SEARCH_ENGINES: dict[str, str] = {
    "google":     "https://www.google.com/search?q=",
    "bing":       "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "yandex":     "https://yandex.com/search/?text=",
}

_MAC_APP_NAMES: dict[str, str] = {
    "chrome":  "Google Chrome",
    "edge":    "Microsoft Edge",
    "firefox": "Firefox",
    "opera":   "Opera",
    "operagx": "Opera GX",
    "brave":   "Brave Browser",
    "vivaldi": "Vivaldi",
    "safari":  "Safari",
}

# Windows registry lookup names for browsers whose spec has no explicit binary
_WIN_EXE_HINTS: dict[str, str] = {"chrome": "chrome", "edge": "msedge"}


def _open_native(url: str, browser_name: Optional[str]) -> str:
    """
    Opens the user's REAL browser normally — with their own profile,
    logged-in accounts and extensions. No automation attaches, so an
    about:blank tab or a blank profile NEVER shows up.
    If url is empty the browser starts with no URL (its own start page /
    session restore) — exactly as if the user had opened it themselves.
    Works on all three of Windows / macOS / Linux.
    """
    url = _normalize_url(url) if url and url.strip() else ""
    if url == "about:blank":
        url = ""

    name = None
    if browser_name:
        name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
    elif not url:
        # No URL → only a window will open; needs the default browser's exe
        name = _detect_default_browser()

    # Specific browser → launch its own executable, exactly like the user would.
    if name:
        if _OS == "Darwin":
            app = _MAC_APP_NAMES.get(name)
            if app:
                cmd = ["open", "-a", app] + ([url] if url else [])
                try:
                    subprocess.run(cmd, check=True, timeout=10)
                    return f"Opened in {name}: {url}" if url else f"Opened {name}."
                except Exception as e:
                    print(f"[Browser] 'open -a {app}' failed ({e}), trying binary…")

        spec = _resolve_browser(name)
        exe  = spec.get("exe") if spec else None
        if not exe and _OS == "Windows":
            if name in ("opera", "operagx"):
                exe = _find_opera_windows()
            else:
                exe = _find_exe_windows(_WIN_EXE_HINTS.get(name, name))
        if exe:
            try:
                subprocess.Popen(
                    [exe, url] if url else [exe],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                return f"Opened in {name}: {url}" if url else f"Opened {name}."
            except Exception as e:
                print(f"[Browser] Native launch failed for {name}: {e}")
        print(f"[Browser] '{name}' not found — falling back to default browser.")

    if not url:
        return "Could not find a browser to open."

    # Default browser via the OS — exactly like the user clicking a link.
    try:
        if _OS == "Windows":
            os.startfile(url)                       # ShellExecute → default browser
        elif _OS == "Darwin":
            subprocess.run(["open", url], check=True, timeout=10)
        else:
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return f"Opened in your default browser: {url}"
    except Exception:
        try:
            if webbrowser.open(url):
                return f"Opened in your default browser: {url}"
        except Exception:
            pass
        return f"Could not open a browser for: {url}"


class _BrowserSession:
    """
    A full session for one browser instance.
    All browsers open on the real profile via launch_persistent_context.
    """

    def __init__(self, browser_name: str, cdp_port: int | None = None):
        self.browser_name = browser_name
        self._spec        = _resolve_browser(browser_name)

        # When set, _launch() uses connect_over_cdp instead of
        # launch_persistent_context — attaching to the browser the user already
        # has open rather than starting a parallel process.
        self._cdp_port: int | None = cdp_port
        self._cdp_browser: Browser | None = None

        self._loop:    asyncio.AbstractEventLoop | None = None
        self._thread:  threading.Thread | None          = None
        self._ready    = threading.Event()

        self._pw:      Playwright     | None = None
        self._context: BrowserContext | None = None
        self._page:    Page           | None = None

        # Task anchoring. The old `_page` + `_first_live_page()` fallback
        # was dangerous on CDP-attached real browsers: if Jarvis's tab
        # closed (user closed it, site navigated away, …), the recovery
        # path could silently adopt an UNRELATED tab the user had open
        # (e.g. ChatGPT) and keep operating on it. Two defences:
        #   * `_task_url` records the URL Jarvis most recently navigated
        #     to. Recovery prefers a live tab whose origin matches.
        #   * `_owned_page_ids` is a set of id(Page) for every tab Jarvis
        #     created or explicitly navigated. Recovery prefers an owned
        #     live sibling before falling back to any live tab.
        # Both lose signal gracefully: if the preferred candidate is
        # gone, the next-best option is still better than `pages[0]`.
        self._task_url: str | None = None
        self._task_origin: str | None = None
        self._owned_page_ids: set[int] = set()

        # Follow-up turns ("continue", "try again", "submit it") need
        # a snapshot of what the last browser action looked at and what
        # it filled. Without this the model has to re-inspect the form
        # on every turn — and one of the real bugs the user hit was
        # re-inspecting the WRONG page after losing the anchor.
        self._last_inspect: str | None = None
        self._last_fill_summary: str | None = None
        self._last_action: str | None = None
        self._last_error: str | None = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"BrowserThread-{self.browser_name}",
        )
        self._thread.start()
        self._ready.wait(timeout=20)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_init())
        self._ready.set()
        self._loop.run_forever()

    async def _async_init(self):
        self._pw = await async_playwright().start()

    def run(self, coro, timeout: int = 60) -> str:
        if not self._loop:
            raise RuntimeError(f"Session for '{self.browser_name}' not started.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._async_close(), self._loop).result(10)

    async def _async_close(self):
        # CDP: never close the real browser; disconnect only. Closing the
        # context would also close the user's tabs.
        if self._cdp_browser is not None:
            try:
                await self._cdp_browser.close()  # disconnects CDP, leaves Chrome alive
            except Exception:
                pass
            self._cdp_browser = None
        elif self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = self._page = None

    def is_dead(self) -> bool:
        """True when this session cannot drive the browser any more —
        the context was closed, the CDP browser disconnected, or the
        Playwright process is gone. The registry calls this before
        returning the cached session; a dead one is evicted so the next
        call reconnects rather than returning BrowserContext errors
        forever. This was the "permanent breakage" bug: the user closed
        an automation-owned window, Playwright tore the context down,
        and every subsequent `_get_page` call returned the same dead
        reference."""
        if self._cdp_browser is not None:
            try:
                if not self._cdp_browser.is_connected():
                    return True
            except Exception:
                return True
        ctx = self._context
        if ctx is None:
            # Never been launched yet — not dead, just not alive.
            return False
        is_closed = getattr(ctx, "is_closed", None)
        if callable(is_closed):
            try:
                if is_closed():
                    return True
            except Exception:
                return True
        browser = getattr(ctx, "browser", None)
        if browser is not None:
            is_connected = getattr(browser, "is_connected", None)
            if callable(is_connected):
                try:
                    if not is_connected():
                        return True
                except Exception:
                    return True
        return False

    def _ctx_alive(self) -> bool:
        """True when the context is usable for a new page. Safeguards every
        `self._context.new_page()` call — the API throws
        'BrowserContext.new_page: Target page, context or browser has been
        closed' otherwise."""
        ctx = self._context
        if ctx is None:
            return False
        # Playwright exposes `is_closed()` on BrowserContext in recent
        # versions; older ones don't. Be defensive.
        is_closed = getattr(ctx, "is_closed", None)
        if callable(is_closed):
            try:
                if is_closed():
                    return False
            except Exception:
                return False
        browser = getattr(ctx, "browser", None)
        if browser is not None:
            is_connected = getattr(browser, "is_connected", None)
            if callable(is_connected):
                try:
                    if not is_connected():
                        return False
                except Exception:
                    return False
        return True

    @staticmethod
    def _origin_of(url: str | None) -> str | None:
        """scheme://host — loose matcher so `example.com/a` and
        `example.com/b/c?q=x` anchor to the same task."""
        if not url:
            return None
        try:
            # Standalone parse to avoid dragging urllib into the hot path
            # when `url` is an easy case ('about:blank', '', None).
            from urllib.parse import urlparse
            p = urlparse(url)
            if not p.scheme or not p.netloc:
                return None
            return f"{p.scheme}://{p.netloc}"
        except Exception:
            return None

    def _remember_navigation(self, page: Page) -> None:
        """Mark a page as task-owned and update the task URL anchor."""
        try:
            self._owned_page_ids.add(id(page))
        except Exception:
            pass
        try:
            url = page.url
        except Exception:
            url = None
        if url and url not in ("about:blank", ""):
            self._task_url = url
            self._task_origin = self._origin_of(url) or self._task_origin

    def _first_live_page(self) -> Page | None:
        """Pick a live page from the context, preferring (in order):
          1. an owned page whose origin matches the current task,
          2. any owned live page,
          3. any live page whose origin matches the task,
          4. any live page at all.

        This prevents the real-browser bug where Jarvis's tab closes and
        the recovery path silently adopts the user's unrelated tab
        (ChatGPT, mail, …) that happens to be earlier in `context.pages`.
        """
        if self._context is None:
            return None
        try:
            pages = [p for p in self._context.pages if not p.is_closed()]
        except Exception:
            return None
        if not pages:
            return None

        origin = getattr(self, "_task_origin", None)
        owned_ids = getattr(self, "_owned_page_ids", set())
        owned = [p for p in pages if id(p) in owned_ids]

        def _origin_match(p: Page) -> bool:
            if not origin:
                return False
            try:
                return self._origin_of(p.url) == origin
            except Exception:
                return False

        for p in owned:
            if _origin_match(p):
                return p
        if owned:
            return owned[0]
        for p in pages:
            if _origin_match(p):
                return p
        return pages[0]

    async def _adopt_page(self) -> Page:
        """
        launch_persistent_context already opens a starting tab.
        Instead of opening a new blank tab (about:blank), it adopts that tab —
        so the user never sees an extra blank tab.
        """
        await asyncio.sleep(0.3)
        live = self._first_live_page()
        if live is not None:
            return live
        if not self._ctx_alive():
            raise RuntimeError("Browser context closed — cannot adopt a page.")
        return await self._context.new_page()

    async def _launch(self):
        """
        Launches the browser with the real user profile.
        Does nothing if the context is already open.
        """
        if self._context is not None:
            return

        # CDP attach: user already started their own browser with
        # --remote-debugging-port — we connect, we do not spawn a new one.
        if self._cdp_port is not None:
            endpoint = _cdp_probe(self._cdp_port)
            if endpoint is None:
                raise RuntimeError(
                    f"CDP port {self._cdp_port} not reachable. "
                    f"{_cdp_launch_hint(self.browser_name, self._cdp_port)}"
                )
            self._cdp_browser = await self._pw.chromium.connect_over_cdp(endpoint)
            # Chrome exposes exactly one default BrowserContext over CDP.
            ctxs = self._cdp_browser.contexts
            self._context = ctxs[0] if ctxs else await self._cdp_browser.new_context()
            self._page = await self._adopt_page()
            print(f"[Browser] 🔗 Attached to running {self.browser_name} on :{self._cdp_port}")
            return

        if self._spec is None:
            raise RuntimeError(
                f"'{self.browser_name}' bu platformda ({_OS}) desteklenmiyor."
            )

        engine_name = self._spec["engine"]
        exe         = self._spec["exe"]
        channel     = self._spec["channel"]
        engine_obj  = getattr(self._pw, engine_name)

        if engine_name == "firefox":
            profile = _firefox_profile_dir() or str(
                Path.home() / ".jarvis_profiles" / "firefox"
            )
            kwargs: dict = {
                "headless":    False,
                "slow_mo":     0,
                "viewport":    None,
                "no_viewport": True,
                "timeout":     25_000,
            }
            if exe:
                kwargs["executable_path"] = exe
            try:
                self._context = await engine_obj.launch_persistent_context(profile, **kwargs)
            except Exception as e:
                print(f"[Browser] Firefox real profile failed ({e}), using JARVIS profile")
                jarvis = str(Path.home() / ".jarvis_profiles" / "firefox_jarvis")
                Path(jarvis).mkdir(parents=True, exist_ok=True)
                self._context = await engine_obj.launch_persistent_context(jarvis, **kwargs)

            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Firefox launched")
            return

        if engine_name == "webkit":
            safari_profile = str(Path.home() / ".jarvis_profiles" / "safari")
            Path(safari_profile).mkdir(parents=True, exist_ok=True)
            kwargs = {
                "headless":    False,
                "slow_mo":     0,
                "viewport":    None,
                "no_viewport": True,
                "timeout":     25_000,
            }
            self._context = await engine_obj.launch_persistent_context(safari_profile, **kwargs)
            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Safari launched")
            return

        profile = _real_profile_dir(self.browser_name)

        kwargs = {
            "headless":    False,
            "slow_mo":     0,
            "viewport":    None,
            "no_viewport": True,
            "timeout":     25_000,
            "args": [
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--disable-default-apps",
                "--no-default-browser-check",
            ],
        }

        if exe:
            kwargs["executable_path"] = exe
        elif channel:
            kwargs["channel"] = channel

        label = (
            f"{self.browser_name}"
            + (f"/{channel}" if channel else "")
            + (f" @ {exe}" if exe else "")
        )

        try:
            self._context = await engine_obj.launch_persistent_context(profile, **kwargs)
            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Launched [{label}] profile={profile}")
            return
        except Exception as e:
            print(f"[Browser] ⚠️  Real profile failed for {label}: {e}")

        # The real profile could not be opened (browser already open / locked
        # profile / newer Chrome versions block the real profile under
        # automation). Fall back to a persistent JARVIS automation profile —
        # accounts logged in here once stay logged in on later sessions too.
        jarvis_profile = str(Path.home() / ".jarvis_profiles" / self.browser_name)
        Path(jarvis_profile).mkdir(parents=True, exist_ok=True)
        print(f"[Browser] Retrying with JARVIS profile: {jarvis_profile}")

        try:
            self._context = await engine_obj.launch_persistent_context(jarvis_profile, **kwargs)
            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Launched [{label}] with JARVIS profile "
                  f"(sign-ins persist across sessions)")
        except Exception as e2:
            raise RuntimeError(f"Could not launch {self.browser_name}: {e2}") from e2


    async def _get_page(self) -> Page:
        await self._launch()
        # If the previous active page was closed externally (user closed
        # the tab, site called window.close, context was swapped) try to
        # pick up an existing live sibling first — creating a blank new
        # page would both lose the user's workflow and crash when the
        # context has itself been closed.
        if self._page is None or self._page.is_closed():
            live = self._first_live_page()
            if live is not None:
                self._page = live
            elif self._ctx_alive():
                self._page = await self._context.new_page()
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError(
                    "BrowserContext has been closed — cannot continue. "
                    "Call action='switch' or 'attach' to re-establish.")
        return self._page

    async def go_to(self, url: str) -> str:

        url      = _normalize_url(url)
        page     = await self._get_page()
        prev_url = page.url

        async def _do_goto(p: Page) -> str:
            """Attempt navigation and return the resulting URL (may still be blank)."""
            try:
                await p.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(0.3)
            except PlaywrightTimeout:
                pass   # page may have partially loaded — check URL below
            except Exception as e:
                print(f"[Browser] goto exception (non-fatal): {e}")
            return p.url

        result_url = await _do_goto(page)

        if result_url in ("about:blank", "", None, prev_url) and prev_url in ("about:blank", "", None):
            print(f"[Browser] Still blank after goto — retrying on new tab: {url}")
            try:
                if not self._ctx_alive():
                    print("[Browser] Skipping new-tab retry — context closed.")
                else:
                    new_page   = await self._context.new_page()
                    self._page = new_page
                    result_url = await _do_goto(new_page)
            except Exception as e:
                print(f"[Browser] New-tab retry failed: {e}")

        if result_url and result_url not in ("about:blank", "", None):
            # Anchor so follow-up tool calls across turns pick THIS tab
            # back up, not an unrelated one the user had open.
            self._remember_navigation(page)
            return f"Opened: {result_url}"
        return f"Could not open: {url}"

    async def search(self, query: str, engine: str = "google") -> str:
        base = _SEARCH_ENGINES.get(engine.lower(), _SEARCH_ENGINES["google"])
        return await self.go_to(base + query.replace(" ", "+"))

    async def click(self, selector: str = None, text: str = None) -> str:
        """Click something. Tries (in order) ARIA role+name, label,
        placeholder, exact text, substring text, aria-label/title/alt
        attributes. If `text` looks like an intent keyword (register,
        login, submit, …) it is first expanded through core/synonyms so
        the same call works on an English or Bengali site.

        Returns a short string naming the strategy that matched, so the
        model can tell "clicked by role=button name='Sign Up'" apart
        from "clicked by substring text 'register'".
        """
        page = await self._get_page()

        if selector and not text:
            try:
                await page.click(selector, timeout=8_000)
                return f"Clicked selector: {selector}"
            except PlaywrightTimeout:
                return f"Selector not found (timeout): {selector}"
            except Exception as e:
                return f"Click error on '{selector}': {e}"

        if not text:
            return "No selector or text provided."

        # Candidate labels: original + synonym expansion if the caller
        # handed us an intent keyword. Users / the model type "sign up"
        # as an intent interchangeably with the literal button text, so
        # we try both paths.
        from core import synonyms as _syn
        candidates: list[str] = [text]
        expanded = _syn.expand(text)
        for e in expanded:
            if e not in candidates:
                candidates.append(e)

        last_err = ""
        for label in candidates:
            for role in ("button", "link", "menuitem", "tab",
                         "checkbox", "radio"):
                try:
                    loc = page.get_by_role(role, name=label, exact=False)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=4_000)
                        return (f"Clicked by role={role} name='{label}'"
                                + (f" (expanded from '{text}')"
                                   if label != text else ""))
                except Exception as e:
                    last_err = str(e)[:120]
            # label / placeholder / substring text / aria-label etc.
            for strategy, cb in (
                ("label",
                 lambda l=label: page.get_by_label(l, exact=False).first
                 .click(timeout=4_000)),
                ("placeholder",
                 lambda l=label: page.get_by_placeholder(l, exact=False).first
                 .click(timeout=4_000)),
                ("text(exact)",
                 lambda l=label: page.get_by_text(l, exact=True).first
                 .click(timeout=4_000)),
                ("text(substr)",
                 lambda l=label: page.get_by_text(l, exact=False).first
                 .click(timeout=4_000)),
                ("aria-attrs",
                 lambda l=label: page.locator(
                     f'[aria-label*="{l}" i],[title*="{l}" i],'
                     f'[alt*="{l}" i]').first.click(timeout=4_000)),
            ):
                try:
                    await cb()
                    return (f"Clicked by {strategy} '{label}'"
                            + (f" (expanded from '{text}')"
                               if label != text else ""))
                except PlaywrightTimeout:
                    continue
                except Exception as e:
                    last_err = str(e)[:120]
                    continue

        return (f"Element not found for '{text}'. Tried {len(candidates)} "
                f"synonym(s) across role/label/placeholder/text/aria. "
                f"Last error: {last_err}" if last_err else
                f"Element not found for '{text}'.")

    async def type_text(self, selector: str = None, text: str = "",
                        clear_first: bool = True) -> str:
        """Prefer `fill()` over keystroke-simulated `type()`. React and
        most modern input components ignore keystrokes but accept
        `fill`'s synthesised input events; the old `.type(delay=50)`
        path was silently dropping values on half the sites we tried."""
        page = await self._get_page()
        try:
            el = page.locator(selector).first if selector else \
                 page.locator(":focus")
            if clear_first:
                try:
                    await el.fill("")
                except Exception:
                    try:
                        await el.clear()
                    except Exception:
                        pass
            try:
                await el.fill(text)
            except Exception:
                # Custom-widget fallback (contenteditable, canvas-backed).
                await el.click()
                await el.type(text, delay=20)
            # Verify when possible.
            try:
                observed = await el.input_value()
                if observed == text:
                    return f"Text typed (verified): {len(text)} chars."
            except Exception:
                pass
            return f"Text typed: {len(text)} chars."
        except Exception as e:
            return f"Type error: {e}"

    async def scroll(self, direction: str = "down", amount: int = 500) -> str:
        page = await self._get_page()
        try:
            y = amount if direction == "down" else -amount
            await page.mouse.wheel(0, y)
            return f"Scrolled {direction}."
        except Exception as e:
            return f"Scroll error: {e}"

    async def press(self, key: str) -> str:
        page = await self._get_page()
        try:
            await page.keyboard.press(key)
            return f"Pressed: {key}"
        except Exception as e:
            return f"Key error: {e}"

    async def get_text(self) -> str:
        page = await self._get_page()
        try:
            text = await page.inner_text("body")
            return text[:4_000]
        except Exception as e:
            return f"Could not get page text: {e}"

    async def get_url(self) -> str:
        page = await self._get_page()
        return page.url

    async def verify_page(self) -> str:
        """Return a short diagnostic the model (or a dashboard panel) can
        read to confirm the current tab is still the one the task is
        operating on. Produced in a consistent one-line-per-fact shape so
        downstream text parsing is trivial.
        """
        page = await self._get_page()
        try:
            url = page.url
        except Exception as e:
            return f"page.url failed: {e}"
        try:
            title = await page.title()
        except Exception:
            title = ""
        origin = self._origin_of(url) or ""
        on_task = (self._task_origin is None or
                   origin == self._task_origin)
        tabs = 0
        try:
            tabs = sum(1 for p in self._context.pages if not p.is_closed())
        except Exception:
            pass
        return (
            "page verify:\n"
            f"  url: {url}\n"
            f"  title: {title[:120]}\n"
            f"  origin: {origin}\n"
            f"  task_url: {self._task_url}\n"
            f"  task_origin: {self._task_origin}\n"
            f"  on_task_url: {on_task}\n"
            f"  owned: {id(page) in self._owned_page_ids}\n"
            f"  tabs_open: {tabs}"
        )

    async def probe_selector(self, selector: str) -> str:
        """Report whether a CSS / aria selector exists and is visible
        on the current page. Returns a one-line probe string the caller
        can grep. This is the "does the stop button exist?" primitive
        — before this, ai_site.py was searching for raw CSS strings
        inside body innerText, which always said no."""
        page = await self._get_page()
        if not selector:
            return "probe: selector=(empty) exists=False"
        try:
            loc = page.locator(selector).first
            count = await loc.count()
            visible = False
            if count > 0:
                try:
                    visible = await loc.is_visible()
                except Exception:
                    visible = False
            return (f"probe: selector={selector!r} exists={count > 0} "
                    f"visible={visible} count={count}")
        except Exception as e:
            return f"probe: selector={selector!r} error={e}"

    async def task_state(self) -> str:
        """Short, structured summary the model reads to answer
        'continue' / 'try again' / 'submit it' without re-scanning the
        page. It names the anchored task URL, the last action performed,
        and the raw last `inspect_form` / `fill_form` outputs (the model
        can scan these for field names / verified counts).
        """
        page = None
        try:
            page = await self._get_page()
            url = page.url
        except Exception as e:
            url = f"(unavailable: {e})"
        snapshot = [
            f"task_url:     {self._task_url}",
            f"task_origin:  {self._task_origin}",
            f"active_url:   {url}",
            f"last_action:  {self._last_action}",
            f"last_error:   {self._last_error}",
        ]
        if self._last_inspect:
            snapshot.append("last_inspect:")
            snapshot.append("  " + self._last_inspect.replace(
                "\n", "\n  "))
        if self._last_fill_summary:
            snapshot.append("last_fill_summary:")
            snapshot.append("  " + self._last_fill_summary.replace(
                "\n", "\n  "))
        return "\n".join(snapshot)

    async def dismiss_dialogs(self) -> str:
        """Try to dismiss cookie banners, consent prompts, and modal
        overlays. Common labels across English and Bengali, limited to a
        small first-click-wins list to avoid dismissing something the
        user actually wanted.

        Returns the label of whatever got clicked (or "nothing to
        dismiss"). This is safe to call once after `go_to` on sites that
        front-load a cookie banner — the banner otherwise blocks every
        subsequent click target.
        """
        page = await self._get_page()
        candidates = [
            # English cookie / consent
            "Accept all", "Accept All", "Accept cookies", "I agree",
            "Agree", "Got it", "OK", "Okay", "Close",
            "Allow all", "Continue",
            # Bengali equivalents
            "সব অনুমতি দিন", "আমি রাজি", "সম্মত", "ঠিক আছে",
            "বন্ধ করুন", "চালিয়ে যান",
            # Reject-style — avoid: we never auto-reject on the user's behalf
            # because many sites behave worse with rejected cookies.
        ]
        for label in candidates:
            for role in ("button", "link"):
                try:
                    loc = page.get_by_role(role, name=label, exact=False)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=2_000)
                        self._last_action = f"dismissed:{label}"
                        return f"Dismissed via {role}: '{label}'"
                except Exception:
                    continue
        # Common modal-close CSS fallbacks.
        for sel in ("[aria-label*='close' i]", ".modal .close",
                    "button[aria-label='Dismiss']"):
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.click(timeout=2_000)
                    self._last_action = f"dismissed:{sel}"
                    return f"Dismissed via CSS: {sel}"
            except Exception:
                continue
        return "Nothing obvious to dismiss."

    async def inspect_form(self, which: str = "primary") -> str:
        """Return a structured dump of the first (or named) form on the
        page: inputs, textareas, selects, radios, checkboxes, buttons.
        Uses page.evaluate so React / dynamic DOMs render the same way
        the user sees them, not a stale snapshot."""
        page = await self._get_page()
        script = """
        (which) => {
          const forms = Array.from(document.querySelectorAll('form'));
          const form  = which === 'primary' ? (forms[0] || document.body)
                                            : (forms.find(f =>
                                                (f.name || '').includes(which)
                                              || (f.id || '').includes(which))
                                               || forms[0] || document.body);
          const bb = form.getBoundingClientRect();
          const visible = (el) => {
            const s = window.getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden'
                || s.opacity === '0') return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };
          const labelFor = (el) => {
            if (el.labels && el.labels.length) {
              return (el.labels[0].innerText || '').trim();
            }
            const al = el.getAttribute('aria-label');
            if (al) return al.trim();
            const ph = el.getAttribute('placeholder');
            if (ph) return ph.trim();
            // Nearest <label> ancestor
            let p = el.parentElement;
            while (p && p !== form) {
              if (p.tagName === 'LABEL') return (p.innerText || '').trim();
              p = p.parentElement;
            }
            return '';
          };
          const describe = (el) => ({
            tag:        el.tagName.toLowerCase(),
            type:       (el.type || '').toLowerCase(),
            name:       el.name || '',
            id:         el.id || '',
            label:      labelFor(el),
            placeholder: el.getAttribute('placeholder') || '',
            required:   !!el.required,
            disabled:   !!el.disabled,
            visible:    visible(el),
            value:      (el.type === 'password') ? '' : (el.value || ''),
            options:    (el.tagName === 'SELECT')
                          ? Array.from(el.options).map(o => o.text).slice(0, 20)
                          : [],
          });
          const fields  = Array.from(form.querySelectorAll(
                              'input, textarea, select'))
                          .filter(e => (e.type || '') !== 'hidden')
                          .map(describe);
          const buttons = Array.from(form.querySelectorAll(
                              'button, [role="button"], input[type="submit"]'))
                          .filter(visible)
                          .map(b => ({
                            text:  (b.innerText || b.value || '').trim(),
                            type:  (b.type || '').toLowerCase(),
                            name:  b.name || '',
                            id:    b.id || '',
                            disabled: !!b.disabled,
                          }));
          return {
            forms_count: forms.length,
            using_body_fallback: forms.length === 0,
            form: {
              action: form.getAttribute('action') || '',
              method: (form.getAttribute('method') || 'get').toLowerCase(),
              name:   form.name || '',
              id:     form.id || '',
            },
            fields:  fields,
            buttons: buttons,
          };
        }
        """
        try:
            data = await page.evaluate(script, which)
        except Exception as e:
            return f"inspect_form error: {e}"
        # Compact human-readable summary. Full JSON is too large for a
        # voice reply; we show counts + a field-by-field table.
        lines = [
            f"Forms on page: {data.get('forms_count', 0)}"
            + (" (using <body> fallback)" if data.get('using_body_fallback')
               else ""),
            f"Form: name='{data['form'].get('name')}' "
            f"id='{data['form'].get('id')}' "
            f"action='{data['form'].get('action')}' "
            f"method={data['form'].get('method')}",
            f"Fields: {len(data['fields'])}",
        ]
        for f in data["fields"][:30]:
            lines.append(
                f"  - {f['tag']}[{f['type'] or '-'}] "
                f"name='{f['name']}' id='{f['id']}' "
                f"label={f['label'][:40]!r} "
                f"required={f['required']} visible={f['visible']}"
                + (f" options={len(f['options'])}" if f['options'] else "")
            )
        lines.append(f"Buttons: {len(data['buttons'])}")
        for b in data["buttons"][:20]:
            lines.append(
                f"  - '{(b['text'] or '').strip()[:40]}' "
                f"type={b['type'] or '-'} disabled={b['disabled']}"
            )
        out = "\n".join(lines)
        self._last_inspect = out
        self._last_action = "inspect_form"
        return out

    async def fill_form(self, fields: dict) -> str:
        """Fill a form and VERIFY each field by reading its value back.

        For each `(key, value)`:
          1. Try to locate the input via role=textbox, get_by_label,
             get_by_placeholder, `[name="key"]`, `[id="key"]`, raw CSS
             selector (so callers can still pass a precise selector).
             Synonym expansion lets "email" also match বাংলা labels.
          2. clear() then fill() — `fill` is the correct Playwright verb
             for `<input>` values; `type` has to simulate keystrokes and
             is slower and less reliable on some React inputs.
          3. Read `locator.input_value()` and compare to the intended
             value. A field whose value does not round-trip is reported
             as 'filled but not verified' with the observed value.

        Password-shaped fields (type=password OR key containing
        "password" / "secret" / "pin" / "otp") are never echoed in the
        return value — only "(masked)" appears.
        """
        page = await self._get_page()
        from core import synonyms as _syn

        async def _resolve_locator(key: str):
            """Return the first live locator matching `key`, trying
            role / label / placeholder / name / id / raw CSS, plus a
            second pass through synonyms."""
            candidates: list[str] = [key]
            exp = _syn.expand(key)
            for e in exp:
                if e not in candidates:
                    candidates.append(e)

            for label in candidates:
                for strategy, build in (
                    ("role=textbox",
                     lambda l=label: page.get_by_role("textbox", name=l)),
                    ("label",
                     lambda l=label: page.get_by_label(l, exact=False)),
                    ("placeholder",
                     lambda l=label: page.get_by_placeholder(l, exact=False)),
                ):
                    try:
                        loc = build().first
                        if await loc.count() > 0:
                            return loc, strategy, label
                    except Exception:
                        continue

            # Attribute / CSS fallbacks using the original key only — these
            # are selector-space, not label-space, so synonyms don't apply.
            for strategy, sel in (
                ("[name]", f'[name="{key}"]'),
                ("[id]",   f'#{key}' if key.replace("_", "").isalnum()
                           else f'[id="{key}"]'),
                ("css",    key),
            ):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        return loc, strategy, key
                except Exception:
                    continue
            return None, None, None

        def _is_secret(key: str) -> bool:
            k = (key or "").lower()
            return any(tok in k for tok in
                       ("password", "pass_", "secret", "otp", "pin",
                        "token", "cvv", "cvc", "card"))

        async def _is_password_input(locator) -> bool:
            try:
                t = (await locator.get_attribute("type") or "").lower()
                return t == "password"
            except Exception:
                return False

        filled: list[str] = []
        verified: list[str] = []
        missed: list[str] = []
        mismatched: list[str] = []

        async def _tag_and_type(locator) -> tuple[str, str]:
            try:
                tag = (await locator.evaluate(
                    "el => el.tagName")).lower()
            except Exception:
                tag = ""
            try:
                t = (await locator.get_attribute("type") or "").lower()
            except Exception:
                t = ""
            return tag, t

        for key, value in fields.items():
            if key == "_missing":
                continue  # convention used by actions/form_fill.py
            value_s = str(value)
            locator, strategy, matched_label = await _resolve_locator(str(key))
            if locator is None:
                missed.append(str(key))
                continue
            try:
                await locator.wait_for(state="visible", timeout=4_000)
            except Exception:
                pass

            tag, input_type = await _tag_and_type(locator)

            # SELECT — use select_option; `value` may be the option
            # text or its value attribute.
            if tag == "select":
                try:
                    try:
                        await locator.select_option(label=value_s)
                    except Exception:
                        await locator.select_option(value=value_s)
                except Exception as e:
                    missed.append(f"{key} (select failed: "
                                  f"{type(e).__name__})")
                    continue
                # Verify selected
                try:
                    actual = await locator.input_value()
                except Exception:
                    actual = None
                if actual == value_s:
                    filled.append(f"{key} [select] = {value_s}")
                    verified.append(str(key))
                else:
                    mismatched.append(f"{key} select wanted={value_s} "
                                      f"got={actual}")
                continue

            # CHECKBOX — bool-coerce value; `check` / `uncheck`.
            if input_type == "checkbox":
                want_on = str(value).lower() in ("true", "1", "yes",
                                                  "on", "checked")
                try:
                    if want_on:
                        await locator.check()
                    else:
                        await locator.uncheck()
                except Exception as e:
                    missed.append(f"{key} (checkbox: "
                                  f"{type(e).__name__})")
                    continue
                try:
                    is_on = await locator.is_checked()
                except Exception:
                    is_on = None
                if is_on == want_on:
                    filled.append(f"{key} [checkbox] = "
                                  f"{'on' if want_on else 'off'}")
                    verified.append(str(key))
                else:
                    mismatched.append(f"{key} checkbox wanted="
                                      f"{want_on} got={is_on}")
                continue

            # RADIO — check the input whose value matches `value_s`.
            if input_type == "radio":
                try:
                    # Scope the search to the group by name attribute,
                    # then match by value or label.
                    name_attr = await locator.get_attribute("name") or ""
                    group_sel = (f'input[type="radio"][name="{name_attr}"]'
                                 if name_attr else 'input[type="radio"]')
                    # Try locating the exact option.
                    option = page.locator(
                        f'{group_sel}[value="{value_s}"]').first
                    if await option.count() == 0:
                        option = page.get_by_label(value_s).first
                    await option.check()
                    is_on = await option.is_checked()
                except Exception as e:
                    missed.append(f"{key} (radio: "
                                  f"{type(e).__name__})")
                    continue
                if is_on:
                    filled.append(f"{key} [radio] = {value_s}")
                    verified.append(str(key))
                else:
                    mismatched.append(f"{key} radio wanted={value_s}")
                continue

            # FILE INPUT — value is a path.
            if input_type == "file":
                try:
                    await locator.set_input_files(value_s)
                    filled.append(f"{key} [file] = {value_s}")
                    verified.append(str(key))
                except Exception as e:
                    missed.append(f"{key} (file: {type(e).__name__})")
                continue

            # Default: text-like input.
            try:
                await locator.fill("")             # clear
                await locator.fill(value_s)        # fill
            except Exception:
                # Fall back to type() for custom-widget inputs.
                try:
                    await locator.click()
                    await locator.type(value_s, delay=20)
                except Exception as e:
                    missed.append(f"{key} ({type(e).__name__})")
                    continue

            secret = _is_secret(str(key)) or await _is_password_input(locator)
            try:
                actual = await locator.input_value()
            except Exception:
                actual = None

            display = "(masked)" if secret else value_s
            if actual == value_s:
                filled.append(f"{key}→{matched_label} [{strategy}] = {display}")
                verified.append(str(key))
            elif actual is None:
                filled.append(f"{key} [{strategy}] = {display} "
                              "(no readback)")
            else:
                actual_display = "(masked)" if secret else str(actual)[:60]
                mismatched.append(f"{key} wanted={display} got={actual_display}")

        total = len(fields) - (1 if "_missing" in fields else 0)
        header = f"Fill summary: {len(verified)}/{total} verified."
        # Front-load the failure state so the caller (and the model
        # reading the result back) cannot mistake a zero-fill for
        # success. The previous output started with "Fill summary: 0/5"
        # which the model occasionally narrated as "I am filling the
        # form" — never again: the first token is now FAILED.
        if total > 0 and len(verified) == 0:
            header = ("FAILED: " + header
                      + " No field locator matched; the form was NOT "
                      "filled. Run action='inspect_form' and retry "
                      "with the exact field names/ids it reports.")
        elif mismatched or missed:
            header = "PARTIAL: " + header

        lines = [header]
        if filled:
            lines.append("Filled & verified: " + "; ".join(filled))
        if mismatched:
            lines.append("Mismatch (value did not round-trip): "
                         + "; ".join(mismatched))
        if missed:
            lines.append("Missed (no locator matched): " + ", ".join(missed))
        out = "\n".join(lines)
        self._last_fill_summary = out
        self._last_action = "fill_form"
        return out

    async def smart_click(self, description: str) -> str:
        page = await self._get_page()
        for role in ("button", "link", "searchbox", "textbox", "menuitem", "tab"):
            try:
                loc = page.get_by_role(role, name=description)
                if await loc.count() > 0:
                    await loc.first.click(timeout=5_000)
                    return f"Clicked ({role}): '{description}'"
            except Exception:
                pass
        for attempt in (
            lambda: page.get_by_text(description, exact=False).first.click(timeout=5_000),
            lambda: page.get_by_placeholder(description, exact=False).first.click(timeout=5_000),
            lambda: page.locator(
                f'[alt*="{description}" i],[title*="{description}" i],'
                f'[aria-label*="{description}" i]'
            ).first.click(timeout=5_000),
        ):
            try:
                await attempt()
                return f"Clicked: '{description}'"
            except Exception:
                pass
        return f"Could not find element: '{description}'"

    async def smart_type(self, description: str, text: str) -> str:
        page = await self._get_page()
        candidates = [
            ("placeholder", page.get_by_placeholder(description, exact=False)),
            ("label",       page.get_by_label(description, exact=False)),
            ("role",        page.get_by_role("textbox", name=description)),
            ("searchbox",   page.get_by_role("searchbox")),
            ("combobox",    page.get_by_role("combobox", name=description)),
        ]
        for method, loc in candidates:
            try:
                el = loc.first
                if await el.count() == 0:
                    continue
                try:
                    await el.fill("")
                    await el.fill(text)
                except Exception:
                    await el.click()
                    await el.type(text, delay=20)
                # Verify
                try:
                    actual = await el.input_value()
                    if actual == text:
                        return (f"Typed into ({method}) '{description}' "
                                f"(verified)")
                except Exception:
                    pass
                return f"Typed into ({method}): '{description}'"
            except Exception:
                continue
        return f"Could not find input: '{description}'"

    async def list_tabs(self) -> str:
        await self._launch()
        pages = list(self._context.pages) if self._context else []
        if not pages:
            return "No open tabs."
        lines = []
        for i, p in enumerate(pages):
            if p.is_closed():
                continue
            try:
                title = await p.title()
            except Exception:
                title = "(no title)"
            marker = " ◀ active" if p is self._page else ""
            lines.append(f"  [{i}] {title[:60]}  —  {p.url[:80]}{marker}")
        return "Open tabs:\n" + "\n".join(lines)

    async def switch_tab(self, index: int | None = None,
                         title_contains: str | None = None) -> str:
        await self._launch()
        pages = [p for p in self._context.pages if not p.is_closed()]
        if not pages:
            return "No open tabs."
        target: Page | None = None
        if index is not None:
            if 0 <= index < len(pages):
                target = pages[index]
            else:
                return f"Tab index {index} out of range (0..{len(pages)-1})."
        elif title_contains:
            needle = title_contains.lower()
            for p in pages:
                try:
                    t = (await p.title()).lower()
                except Exception:
                    t = ""
                if needle in t or needle in p.url.lower():
                    target = p
                    break
            if target is None:
                return f"No tab matches: '{title_contains}'"
        else:
            return "Specify index or title_contains."
        # Confirm target is still live before adopting it; a tab the user
        # closed between `list` and `switch` would otherwise poison self._page.
        if target.is_closed():
            self._page = self._first_live_page()
            return "Chosen tab was closed; adopted the next live one."
        self._page = target
        try:
            await target.bring_to_front()
        except Exception:
            pass
        try:
            title = await target.title()
        except Exception:
            title = target.url
        return f"Switched to tab: {title[:60]}"

    async def new_tab(self, url: str = "") -> str:
        await self._launch()
        if not self._ctx_alive():
            raise RuntimeError(
                "BrowserContext has been closed — cannot open a new tab.")
        new = await self._context.new_page()
        self._page = new
        self._owned_page_ids.add(id(new))
        if url:
            return await self.go_to(url)
        return "New tab opened."

    async def close_tab(self) -> str:
        page = self._page
        if page and not page.is_closed():
            await page.close()
            # Pick the next live sibling. Prior code took `pages[-1]`
            # unconditionally and happily assigned a closed Page back to
            # self._page when the just-closed tab was the last one.
            self._page = self._first_live_page()
            return "Tab closed."
        # Even when _page was stale, we may still have live siblings.
        self._page = self._first_live_page()
        return "No active tab to close." if self._page is None \
               else "Cleaned stale page reference; active tab recovered."

    async def screenshot(self, path: str = None) -> str:
        page = await self._get_page()
        try:
            save_path = path or str(Path.home() / "Desktop" / "jarvis_screenshot.png")
            await page.screenshot(path=save_path, full_page=False)
            return f"Screenshot saved: {save_path}"
        except Exception as e:
            return f"Screenshot error: {e}"

    async def back(self) -> str:
        page = await self._get_page()
        try:
            await page.go_back(timeout=10_000)
            return f"Navigated back: {page.url}"
        except Exception as e:
            return f"Back error: {e}"

    async def forward(self) -> str:
        page = await self._get_page()
        try:
            await page.go_forward(timeout=10_000)
            return f"Navigated forward: {page.url}"
        except Exception as e:
            return f"Forward error: {e}"

    async def reload(self) -> str:
        page = await self._get_page()
        try:
            await page.reload(timeout=15_000)
            return f"Page reloaded: {page.url}"
        except Exception as e:
            return f"Reload error: {e}"

    async def close_browser(self) -> str:
        await self._async_close()
        return f"{self.browser_name} closed."

class _SessionRegistry:
    """Manages all active browser sessions."""

    def __init__(self):
        self._sessions:        dict[str, _BrowserSession] = {}
        self._active_browser:  str                        = ""
        self._lock             = threading.Lock()
        self._last_native_url: str                        = ""
        # Per-process consent — the user says yes once per browser per session.
        # Not persisted: a new JARVIS launch asks again.
        self._consent:         dict[str, bool]            = {}

    def has(self, browser_name: str | None = None) -> bool:
        """Is there an active automation session for this browser (or any)?"""
        with self._lock:
            if not browser_name:
                return bool(self._sessions)
            name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
            return name in self._sessions

    def note_native_url(self, url: str) -> None:
        self._last_native_url = url

    def pop_native_url(self) -> str:
        """Returns the last natively-opened URL once (consumed to avoid repeats)."""
        url, self._last_native_url = self._last_native_url, ""
        return url

    def consented(self, browser_name: str) -> bool:
        name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        with self._lock:
            return bool(self._consent.get(name))

    def grant(self, browser_name: str) -> None:
        name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        with self._lock:
            self._consent[name] = True

    def _get_or_create(self, browser_name: str,
                        cdp_port: int | None = None) -> _BrowserSession:
        with self._lock:
            cached = self._sessions.get(browser_name)
            # Evict dead sessions. Previously the cached session was
            # returned unconditionally — so when the user closed an
            # automation-owned window and Playwright tore down its
            # context, every later call returned the SAME dead session
            # and "BrowserContext has been closed" became permanent.
            if cached is not None:
                try:
                    dead = cached.is_dead()
                except Exception:
                    dead = True
                if dead:
                    print(f"[Registry] Evicting dead session: {browser_name}")
                    try:
                        cached.close()
                    except Exception:
                        pass
                    self._sessions.pop(browser_name, None)
                    cached = None
            if cached is None:
                sess = _BrowserSession(browser_name, cdp_port=cdp_port)
                sess.start()
                self._sessions[browser_name] = sess
                print(f"[Registry] New session: {browser_name}"
                      + (f" (CDP :{cdp_port})" if cdp_port else ""))
            return self._sessions[browser_name]

    def evict(self, browser_name: str) -> None:
        """Force-drop a session reference (e.g. after a terminal
        BrowserContext error). Does not call close() — the context is
        already gone; close() would just raise."""
        with self._lock:
            self._sessions.pop(browser_name, None)
            if self._active_browser == browser_name:
                self._active_browser = ""

    def get(self, browser_name: str | None = None,
             cdp_port: int | None = None) -> _BrowserSession:
        if not browser_name:
            browser_name = self._active_browser or _detect_default_browser()
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        sess = self._get_or_create(browser_name, cdp_port=cdp_port)
        self._active_browser = browser_name
        return sess

    def switch(self, browser_name: str) -> str:
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        self._get_or_create(browser_name)
        self._active_browser = browser_name
        return f"Active browser → {browser_name}"

    def close_one(self, browser_name: str) -> str:
        with self._lock:
            sess = self._sessions.pop(browser_name, None)
        if sess:
            sess.close()
            if self._active_browser == browser_name:
                self._active_browser = ""
            return f"{browser_name} closed."
        return f"No active session for: {browser_name}"

    def close_all(self) -> str:
        with self._lock:
            names    = list(self._sessions.keys())
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._active_browser = ""
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass
        return "All browsers closed: " + (", ".join(names) if names else "none")

    def list_sessions(self) -> str:
        """
        Lists every browser the user has open, not just the ones JARVIS has
        attached to. Playwright-controlled sessions were the only thing shown
        before, which read as "no browsers open" when the user obviously had
        Chrome on screen — confusing and wrong.
        """
        with self._lock:
            attached = dict(self._sessions)
            active   = self._active_browser

        running = _detect_running_browsers()

        lines: list[str] = []
        if attached:
            lines.append("JARVIS-controlled:")
            for name in attached:
                mk = " ◀ active" if name == active else ""
                lines.append(f"  • {name}{mk}")
        if running:
            lines.append("Running on your computer:")
            for name in sorted(running):
                attached_mk = "  (controllable — call attach)" if name not in attached else ""
                lines.append(f"  • {name}{attached_mk}")
        if not lines:
            return "No browsers detected."
        return "\n".join(lines)


_registry = _SessionRegistry()

# Actions that neither open a session nor touch the user's browser — safe to
# run without the "may I access your browser?" gate.
_NO_CONSENT_ACTIONS = {
    "switch", "list_browsers", "close", "close_all", "attach_help",
}

# Actions that submit data, download, or spend money — must go through
# core/confirm.py before they run.
_SENSITIVE_ACTIONS = {"fill_form", "download"}


def _gate_permission(browser: str | None, action: str,
                     parameters: dict) -> str | None:
    """
    First-touch permission gate. Returns a string the model should speak if
    consent is still needed (and parks the actual work behind the HUD banner),
    or None if the action may proceed immediately.
    """
    if action in _NO_CONSENT_ACTIONS:
        return None
    name = _ALIASES.get(
        (browser or _registry._active_browser or _detect_default_browser())
        .lower().strip(),
        (browser or _registry._active_browser or _detect_default_browser()).lower().strip(),
    )
    if _registry.consented(name):
        return None
    if _trust_me():
        # Trust mode: this is the user's machine; they've said once that
        # browser access is pre-approved. Record it so later actions skip
        # even the probe.
        _registry.grant(name)
        return None
    if _confirm is None or not _confirm.is_bound():
        # No HUD bound (headless / test harness). Grant implicitly rather than
        # jamming the assistant.
        _registry.grant(name)
        return None

    def _on_accept() -> str:
        _registry.grant(name)
        # Re-dispatch the exact same call now that consent is granted.
        return browser_control(parameters)

    return _confirm.request(
        key=f"browser-access-{name}",
        title="Access your personal browser",
        detail=f"Allow JARVIS to control {name}?",
        run=_on_accept,
    )


def _gate_sensitive(action: str, parameters: dict,
                    run: Callable[[], str]) -> str:
    """Park a sensitive action behind the HUD banner. Returns the sentence
    the model should speak while the user confirms.

    Trust mode skips the gate: filling a form is not submitting it (we type
    into inputs, the user still clicks Send), so trust_me=True is explicit
    permission to do that without a confirmation each time. A separate 'submit'
    action would gate on its own if we ever add one."""
    if _trust_me() or _is_scoped_trusted():
        return run()
    if _confirm is None or not _confirm.is_bound():
        return run()
    detail = ""
    if action == "fill_form":
        fields = parameters.get("fields", {}) or {}
        detail = f"Fill {len(fields)} field(s)."
    elif action == "download":
        detail = f"Download: {parameters.get('url', '')[:120]}"
    return _confirm.request(
        key=f"browser-{action}",
        title=f"Browser: {action}",
        detail=detail or action,
        run=run,
    )


# Actions that AppleScript can honour on the user's RUNNING Chrome/Brave/Edge
# without any CDP relaunch. Everything else (fill_form, screenshot,
# new_tab/close_tab across windows, back/forward...) still goes to Playwright.
_APPLESCRIPT_ACTIONS = {
    "get_text", "get_url", "scroll", "list_tabs", "switch_tab",
    "press", "click", "smart_click",
    "go_to", "navigate", "search", "new_tab", "close_tab",
    "back", "forward", "reload", "screenshot",
    "type", "smart_type", "fill_form",
    # Human-like control (System Events): bypasses the Chrome JS-pref entirely.
    "keystroke", "shortcut", "find", "find_close",
    "next_tab", "prev_tab", "reopen_tab",
    "enable_js", "focus_address",
    # State + pointer
    "get_state", "hover", "coord_click",
}


def _try_applescript(browser: str | None, action: str, params: dict) -> str | None:
    """Returns a result string if AppleScript handled the action on the
    user's real browser; None if it couldn't (not macOS, browser unknown,
    not running, or AppleScript errored) so the caller falls through to
    Playwright."""
    if _OS != "Darwin" or _macchrome is None:
        return None
    if action not in _APPLESCRIPT_ACTIONS:
        return None
    name = _ALIASES.get(
        (browser or _registry._active_browser or _detect_default_browser())
        .lower().strip(),
        (browser or _registry._active_browser or _detect_default_browser()).lower().strip(),
    )
    if _macchrome.app_name(name) is None:
        return None  # Firefox/Safari: no Chromium AppleScript dict
    # Navigation actions auto-launch the browser if not running; everything
    # else needs an existing window to target.
    _launching = action in ("go_to", "navigate", "search", "new_tab")
    if not _launching and not _macchrome.is_running(name):
        return None  # Nothing to control
    try:
        if action == "get_url":
            return _macchrome.active_url(name)
        if action == "get_text":
            return _macchrome.get_text(name, limit=int(params.get("limit", 4000)))
        if action == "scroll":
            direction = params.get("direction", "down")
            amount = int(params.get("amount", 600))
            try:
                return _macchrome.scroll(name, direction=direction, amount=amount)
            except Exception as _e:
                # JS-from-AppleEvents is off — fall back to Page Down/Up keys.
                pages = max(1, amount // 600)
                return _macchrome.scroll_keys(name, direction=direction, pages=pages)
        if action in ("go_to", "navigate"):
            url = params.get("url", "").strip()
            # Prefer the AppleScript-native URL set (instant, no focus steal).
            # If it fails because JS/AS is clamped on this profile, fall back
            # to the human path: Cmd+L, type, Enter — which always works.
            try:
                return _macchrome.navigate(name, url)
            except Exception:
                return _macchrome.go_to_via_address_bar(name, url)
        if action == "focus_address":
            return _macchrome.shortcut(name, "l", ("cmd",))
        if action == "keystroke":
            return _macchrome.keystroke(name, params.get("text", ""))
        if action == "shortcut":
            key  = params.get("key", "")
            mods = params.get("modifiers") or params.get("mods") or []
            if isinstance(mods, str):
                mods = [m.strip() for m in mods.split("+") if m.strip()]
            return _macchrome.shortcut(name, key, tuple(mods) or ("cmd",))
        if action == "find":
            return _macchrome.find_in_page(name, params.get("text", ""))
        if action == "find_close":
            return _macchrome.close_find(name)
        if action == "next_tab":
            return _macchrome.next_tab(name)
        if action == "prev_tab":
            return _macchrome.prev_tab(name)
        if action == "reopen_tab":
            return _macchrome.reopen_tab(name)
        if action == "enable_js":
            return _macchrome.enable_js_from_apple_events(name)
        if action == "search":
            engine = (params.get("engine") or "google").lower()
            base = _SEARCH_ENGINES.get(engine, _SEARCH_ENGINES["google"])
            q = params.get("query", "").strip()
            if not q:
                return "Empty search query."
            return _macchrome.navigate(name, base + q.replace(" ", "+"))
        if action == "new_tab":
            return _macchrome.new_tab(name, params.get("url", "").strip())
        if action == "close_tab":
            return _macchrome.close_tab(name)
        if action == "back":
            return _macchrome.back(name)
        if action == "forward":
            return _macchrome.forward(name)
        if action == "reload":
            return _macchrome.reload(name)
        if action == "screenshot":
            return _macchrome.screenshot(name, params.get("path"))
        if action == "type" or action == "smart_type":
            text = params.get("text", "")
            if not text:
                return "No text to type."
            try:
                return _macchrome.type_text(
                    name,
                    selector=params.get("selector"),
                    text=text,
                    clear_first=bool(params.get("clear_first", True)),
                )
            except Exception:
                # JS blocked — type into whatever is currently focused via
                # real OS keystrokes. Caller should Tab/click into the field
                # first (or set 'selector' once JS pref is on).
                return _macchrome.keystroke(name, text)
        if action == "fill_form":
            raw_fields = params.get("fields", {}) or {}
            use_prefs = bool(params.get("use_prefs", True))
            fields = _merge_prefs_into_fields(raw_fields) if use_prefs else dict(raw_fields)
            def _do_fill() -> str:
                return _macchrome.fill_form(name, fields)
            return _gate_sensitive("fill_form", params, _do_fill)
        if action == "get_state":
            try:
                url = _macchrome.active_url(name)
            except Exception:
                url = ""
            try:
                title = _macchrome.active_title(name)
            except Exception:
                title = ""
            try:
                tabs = _macchrome.list_tabs(name)
            except Exception:
                tabs = []
            running = _macchrome.is_running(name)
            return (f"browser={name}; running={running}; url={url}; "
                    f"title={title[:100]}; tabs={len(tabs)}")
        if action == "hover" or action == "coord_click":
            # Human-like pointer action: locate element via CSS or text, read
            # its page-space rect with JS, convert to screen coords via the
            # window position/zoom, then use `cliclick` (preferred, pass-
            # through) or AppleScript System Events to move/click there.
            target = params.get("selector") or params.get("text") or ""
            if not target:
                return "Provide 'selector' or 'text' to point at."
            rect_js = _REACT_RECT_JS + f"""
            (function(){{
              var sel = {_js_str(target)};
              var el = null;
              try {{ el = document.querySelector(sel); }} catch(e) {{}}
              if (!el) {{
                var t = sel.toLowerCase();
                el = Array.from(document.querySelectorAll('a,button,[role="button"],input,[onclick]'))
                  .find(function(e){{ return (e.innerText||e.value||'').toLowerCase().includes(t) && e.offsetParent !== null; }});
              }}
              if (!el) return 'NOT_FOUND';
              el.scrollIntoView({{block:'center', inline:'center', behavior:'instant'}});
              var r = el.getBoundingClientRect();
              return JSON.stringify({{
                x: Math.round(r.left + r.width/2),
                y: Math.round(r.top  + r.height/2),
                w: Math.round(r.width), h: Math.round(r.height),
                sx: window.screenX, sy: window.screenY,
                iw: window.innerWidth, ih: window.innerHeight,
                ow: window.outerWidth, oh: window.outerHeight,
                dpr: window.devicePixelRatio || 1
              }});
            }})()"""
            try:
                raw = _macchrome.execute_js(name, rect_js)
            except Exception as e:
                return f"Cannot locate target (JS blocked?): {e}"
            if (raw or "").strip() == "NOT_FOUND":
                return f"Could not find: {target}"
            import json as _json
            try:
                r = _json.loads(raw)
            except Exception:
                return f"Unexpected locate response: {raw[:120]}"
            # Chrome window chrome (title + tab + address bar) sits between
            # window.screenY and the viewport. Infer its height from outer
            # vs inner height — close enough for center clicks.
            chrome_h = max(0, int(r["oh"]) - int(r["ih"]))
            px = int(r["sx"]) + int(r["x"])
            py = int(r["sy"]) + chrome_h + int(r["y"])
            if action == "hover":
                return _os_pointer(px, py, click=False)
            return _os_pointer(px, py, click=True)
        if action == "list_tabs":
            tabs = _macchrome.list_tabs(name)
            if not tabs:
                return "No open tabs."
            lines = []
            for i, t in enumerate(tabs):
                mk = " ◀ active" if t["active"] else ""
                lines.append(f"  [{i}] {t['title'][:60]}  —  {t['url'][:80]}{mk}")
            return "Open tabs:\n" + "\n".join(lines)
        if action == "switch_tab":
            idx = params.get("index")
            return _macchrome.switch_tab(
                name,
                index=int(idx) if idx is not None else None,
                title_contains=params.get("title_contains"),
            )
        if action == "click" or action == "smart_click":
            text = params.get("text") or params.get("description") or ""
            if not text:
                return None  # selector-based clicks are more precise via Playwright
            return _macchrome.click_text(name, text)
        if action == "press":
            key = (params.get("key") or "Enter").lower()
            js_map = {
                "enter":  "document.activeElement && document.activeElement.form && document.activeElement.form.requestSubmit && document.activeElement.form.requestSubmit();",
                "escape": "document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}));",
                "tab":    "document.activeElement && (function(){const els=Array.from(document.querySelectorAll('input,select,textarea,button,a,[tabindex]'));const i=els.indexOf(document.activeElement);if(i>=0&&i+1<els.length)els[i+1].focus();})();",
            }
            if key in js_map:
                _macchrome.execute_js(name, js_map[key])
                return f"Pressed: {key}"
            return None  # unknown key — fall back to Playwright
    except Exception as e:
        err = str(e)
        print(f"[Browser][AppleScript] {name} {action} failed: {err[:200]}")
        # -1743 = macOS Automation permission denied. First-run case — tell
        # the user to allow it in System Settings, then fall through.
        if "-1743" in err:
            return (f"macOS blocked AppleScript access to {name}. "
                    f"Open System Settings → Privacy & Security → Automation, "
                    f"find your Terminal / Python app, and enable {_macchrome.app_name(name)}. "
                    f"Then try again.")
        # Chrome pref "Allow JavaScript from Apple Events" is off. Returning the
        # hint rather than falling through stops the caller from doing a CDP
        # relaunch (which would also fail) — user just needs one menu click.
        if ("Allow JavaScript from Apple Events" in err
                or "JavaScript through AppleScript is turned off" in err):
            return err
        return None
    return None


def browser_control(
    parameters:    dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params  = parameters or {}
    action  = params.get("action", "").lower().strip()
    browser = params.get("browser", "").lower().strip() or None
    result  = "Unknown action."

    # Permission gate — asked once per browser per JARVIS session. Returns a
    # [CONFIRMATION_PENDING] sentence; the real work runs when the user clicks.
    gated = _gate_permission(browser, action, params)
    if gated is not None:
        _log(player, "Awaiting browser-access consent.")
        return gated

    if action == "switch":
        target = browser or params.get("target", "").lower().strip()
        result = _registry.switch(target) if target else "Please specify a browser."
        _log(player, result)
        return result

    if action == "list_browsers":
        result = _registry.list_sessions()
        _log(player, result)
        return result

    if action == "close_all":
        result = _registry.close_all()
        _log(player, result)
        return result

    if action == "close":
        target = browser or _registry._active_browser
        result = _registry.close_one(target) if target else "No browser specified."
        _log(player, result)
        return result

    if action == "attach":
        port = int(params.get("port", _CDP_DEFAULT_PORT))
        target = browser or _detect_default_browser()
        if _cdp_probe(port) is None:
            result = (f"Cannot attach — nothing listening on CDP port {port}. "
                      + _cdp_launch_hint(target, port))
            _log(player, result)
            return result
        try:
            sess = _registry.get(target, cdp_port=port)
            # Force the launch so attach failures surface here, not on next action.
            sess.run(sess.get_url())
            result = f"Attached to running {target} on :{port}."
        except Exception as e:
            result = f"Attach failed: {e}"
        _log(player, result)
        return result

    if action == "start_debug":
        target = browser or _detect_default_browser()
        port   = int(params.get("port", _CDP_DEFAULT_PORT))
        # Destructive — closes the user's running browser. Must confirm on HUD.
        if _confirm is not None and _confirm.is_bound():
            def _do_start() -> str:
                return _start_debug_browser(target, port)
            result = _confirm.request(
                key=f"browser-debug-{target}",
                title=f"Relaunch {target} in control mode",
                detail=(f"Quit any open {target} windows and reopen it in debug "
                        f"mode on port {port} so I can control it directly. "
                        f"Your real profile and logins are preserved."),
                run=_do_start,
            )
        else:
            result = _start_debug_browser(target, port)
        _log(player, result)
        return result

    if action == "trust_site":
        host = (params.get("host") or "").lower().strip()
        if not host:
            # Use current browser's active URL host.
            try:
                from actions import _mac_chrome as _mc
                host = _host_from_url(_mc.active_url("chrome")).lower()
            except Exception:
                host = ""
        if not host:
            result = "Specify 'host' (e.g. rootdevs.com or *.example.com)."
        else:
            hs = _scoped_trust_hosts()
            hs.add(host)
            _save_scoped_trust(hs)
            result = f"Trusted {host} — form fills won't ask for confirmation there."
        _log(player, result)
        return result

    if action == "untrust_site":
        host = (params.get("host") or "").lower().strip()
        hs = _scoped_trust_hosts()
        if host in hs:
            hs.discard(host)
            _save_scoped_trust(hs)
            result = f"Removed {host} from trusted sites."
        else:
            result = f"{host or '(empty)'} is not on the trust list."
        _log(player, result)
        return result

    if action == "list_trusted_sites":
        hs = _scoped_trust_hosts()
        result = ("Trusted sites: " + ", ".join(sorted(hs))) if hs \
            else "No sites are currently trusted."
        _log(player, result)
        return result

    if action == "set_pref":
        key = (params.get("key") or "").strip()
        val = params.get("value", "")
        if not key:
            return "Specify 'key' (e.g. name, email, phone)."
        prefs = _load_prefs()
        if val in (None, "", False):
            prefs.pop(key, None)
            _save_prefs(prefs)
            result = f"Cleared preference '{key}'."
        else:
            prefs[key] = str(val)
            _save_prefs(prefs)
            result = f"Saved preference '{key}'."
        _log(player, result)
        return result

    if action == "get_prefs":
        prefs = _load_prefs()
        if not prefs:
            result = "No browser preferences saved. Use action='set_pref' with key+value to save."
        else:
            lines = [f"  {k}: {v}" for k, v in prefs.items()]
            result = "Saved browser preferences:\n" + "\n".join(lines)
        _log(player, result)
        return result

    if action == "clear_prefs":
        _save_prefs({})
        result = "Cleared all browser preferences."
        _log(player, result)
        return result

    if action == "trust_me":
        sub = (params.get("mode") or params.get("state") or "status").lower().strip()
        if sub in ("on", "enable", "true", "yes"):
            _set_trust(True)
            result = ("Trust mode on. I'll stop asking you for permission "
                      "before touching your browser or camera. Destructive "
                      "actions (closing your Chrome, submitting forms, "
                      "shutting down) will still ask.")
        elif sub in ("off", "disable", "false", "no"):
            _set_trust(False)
            result = "Trust mode off. I'll ask before accessing your browser or camera."
        else:
            result = f"Trust mode is {'ON' if _trust_me() else 'OFF'}."
        _log(player, result)
        return result

    if action == "attach_help":
        target = browser or _detect_default_browser()
        port   = int(params.get("port", _CDP_DEFAULT_PORT))
        result = _cdp_launch_hint(target, port)
        _log(player, result)
        return result

    # ── Navigation is ALWAYS native ──────────────────────────────────────────
    # go_to / search / new_tab open the site in the user's own browser —
    # their own profile, logged-in accounts and start page; exactly as if the
    # user had opened it themselves. A controlled window with about:blank never
    # opens here. The only exception: if an automation flow is already running,
    # navigation continues in that window (so multi-step tasks aren't split).
    if action in ("go_to", "search", "new_tab"):
        if _registry.has(browser):
            sess = _registry.get(browser)
            try:
                if action == "search":
                    result = sess.run(sess.search(params.get("query", ""),
                                                  params.get("engine", "google")))
                elif action == "new_tab":
                    result = sess.run(sess.new_tab(params.get("url", "")))
                else:
                    result = sess.run(sess.go_to(params.get("url", "")))
            except concurrent.futures.TimeoutError:
                result = f"Browser action '{action}' timed out (60s)."
            except Exception as e:
                err_text = str(e)
                low = err_text.lower()
                if any(tok in low for tok in (
                        "browsercontext has been closed",
                        "target page, context or browser has been closed",
                        "browser has been closed",
                        "context has been closed")):
                    try:
                        target_name = _ALIASES.get(
                            (browser or _registry._active_browser
                             or _detect_default_browser()
                             ).lower().strip(),
                            (browser or _registry._active_browser
                             or _detect_default_browser()
                             ).lower().strip(),
                        )
                        _registry.evict(target_name)
                        print(f"[Registry] Auto-evicted {target_name} "
                              f"after dead-context on '{action}'.")
                    except Exception:
                        pass
                    result = (f"Browser session terminated (automation "
                              f"window likely closed). Cleared dead "
                              f"reference. Call action='attach' to "
                              f"reconnect, or retry '{action}' to open "
                              f"a fresh session. Error: {err_text[:160]}")
                else:
                    result = f"Browser error ({action}): {err_text}"
            _log(player, result)
            return result

        if action == "search":
            base    = _SEARCH_ENGINES.get(params.get("engine", "google").lower(),
                                          _SEARCH_ENGINES["google"])
            nav_url = base + params.get("query", "").replace(" ", "+")
        else:
            nav_url = params.get("url", "").strip()

        result = _open_native(nav_url, browser)
        if result.startswith("Opened") and nav_url:
            _registry.note_native_url(_normalize_url(nav_url))
        _log(player, result)
        return result

    # ── Interactive actions (click/type/read…) ───────────────────────────────
    # These require a physically controllable browser; the automation window
    # only opens here, and as soon as it opens it goes to the user's last
    # navigated page — it doesn't sit on a blank page.
    #
    # On macOS, Chromium browsers ship an AppleScript dictionary that lets us
    # read the active tab and run JS in it without CDP. For read/scroll/tab
    # actions this is the ideal path: no relaunch, no isolated profile, works
    # on the user's actual logged-in tab. Try that first; fall through only
    # if it's unavailable or returns None.
    _as_result = _try_applescript(browser, action, params)
    if _as_result is not None:
        _log(player, _as_result)
        return _as_result

    # DOM-level verbs require a live Playwright session — AppleScript cannot
    # run `page.evaluate` or `locator.input_value`. If we don't already have
    # a session, DO NOT silently launch a fresh Playwright window (that was
    # the "a new browser opens for inspect" bug). Tell the user how to
    # attach to their real browser instead.
    _DOM_ONLY = {"inspect_form", "verify_page", "task_state",
                 "dismiss_dialogs", "fill_form", "smart_click",
                 "smart_type", "probe_selector"}
    if action in _DOM_ONLY and not _registry.has(
            _ALIASES.get(
                (browser or _registry._active_browser
                 or _detect_default_browser()).lower().strip(),
                (browser or _registry._active_browser
                 or _detect_default_browser()).lower().strip(),
            )):
        target_for_msg = (browser or _registry._active_browser
                          or _detect_default_browser())
        result = (
            f"Cannot run '{action}' — no controllable Playwright session "
            f"for {target_for_msg} yet. Call action='attach' (if the "
            f"browser is running with CDP on port {_CDP_DEFAULT_PORT}) or "
            f"action='start_debug' (relaunches the browser in control "
            f"mode, keeping your profile and logins). I will not open a "
            f"separate automation window — that would operate on a tab "
            f"you cannot see."
        )
        _log(player, result)
        return result

    # If the user asked us to "scroll MY browser" and we silently launch a
    # fresh Playwright window, the scroll goes to a window they can't see —
    # exactly the "koronai" bug. Before any interactive action: if the user
    # already has that browser running and we haven't attached yet, try CDP
    # on the default port; if the port is closed, stop and tell the user how
    # to open it rather than scrolling a ghost tab.
    target_name = _ALIASES.get(
        (browser or _registry._active_browser or _detect_default_browser())
        .lower().strip(),
        (browser or _registry._active_browser or _detect_default_browser()).lower().strip(),
    )
    if not _registry.has(target_name) and target_name in _detect_running_browsers():
        endpoint = _cdp_probe(_CDP_DEFAULT_PORT)
        if endpoint is not None:
            try:
                sess_pre = _registry.get(target_name, cdp_port=_CDP_DEFAULT_PORT)
                sess_pre.run(sess_pre.get_url())
                print(f"[Browser] 🔗 Auto-attached to running {target_name}")
            except Exception as e:
                print(f"[Browser] Auto-attach failed ({e}) — falling back to launch.")
        elif _OS == "Darwin" and _macchrome is not None and _macchrome.app_name(target_name) is not None:
            # macOS Chromium browser is running and AppleScript can drive it
            # for every read/click/type/scroll/navigate action. The CDP relaunch
            # nudge used to live here, but Chrome M136+ refuses to expose the
            # debug port on the default profile — the relaunch never attaches,
            # just closes the user's browser in a loop. _try_applescript above
            # already handled whatever it can; fall through only for the few
            # actions AppleScript cannot do (e.g. true DevTools features).
            print(f"[Browser] AppleScript bridge covers {target_name}; skipping CDP nudge.")
        else:
            result = (
                f"You already have {target_name} open, but I can't control it "
                f"directly yet. Say 'relaunch {target_name} in control mode' "
                f"(or call start_debug) and I'll close it and reopen it with "
                f"your real profile so I can act on your actual tabs. Your "
                f"logins and extensions stay intact."
            )
            _log(player, result)
            return result

    try:
        sess = _registry.get(browser)
    except Exception as e:
        result = f"Could not start browser session: {e}"
        _log(player, result)
        return result

    try:
        last = _registry.pop_native_url()
        if last:
            try:
                sess.run(sess.go_to(last))
            except Exception as e:
                print(f"[Browser] Could not resume last page ({last}): {e}")

        if action == "click":
            result = sess.run(sess.click(params.get("selector"), params.get("text")))
        elif action == "type":
            result = sess.run(sess.type_text(
                params.get("selector"), params.get("text", ""), params.get("clear_first", True)))
        elif action == "scroll":
            result = sess.run(sess.scroll(params.get("direction", "down"), int(params.get("amount", 500))))
        elif action == "fill_form":
            fields = params.get("fields", {}) or {}
            def _do_fill() -> str:
                return sess.run(sess.fill_form(fields))
            result = _gate_sensitive("fill_form", params, _do_fill)
        elif action == "list_tabs":
            result = sess.run(sess.list_tabs())
        elif action == "switch_tab":
            idx = params.get("index")
            result = sess.run(sess.switch_tab(
                index=int(idx) if idx is not None else None,
                title_contains=params.get("title_contains"),
            ))
        elif action == "smart_click":
            result = sess.run(sess.smart_click(params.get("description", "")))
        elif action == "smart_type":
            result = sess.run(sess.smart_type(params.get("description", ""), params.get("text", "")))
        elif action == "get_text":
            result = sess.run(sess.get_text())
        elif action == "get_url":
            result = sess.run(sess.get_url())
        elif action == "verify_page":
            result = sess.run(sess.verify_page())
        elif action == "inspect_form":
            result = sess.run(sess.inspect_form(
                params.get("which", "primary")))
        elif action == "task_state":
            result = sess.run(sess.task_state())
        elif action == "dismiss_dialogs":
            result = sess.run(sess.dismiss_dialogs())
        elif action == "probe_selector":
            result = sess.run(sess.probe_selector(
                params.get("selector", "")))
        elif action == "press":
            result = sess.run(sess.press(params.get("key", "Enter")))
        elif action == "close_tab":
            result = sess.run(sess.close_tab())
        elif action == "screenshot":
            result = sess.run(sess.screenshot(params.get("path")))
        elif action == "back":
            result = sess.run(sess.back())
        elif action == "forward":
            result = sess.run(sess.forward())
        elif action == "reload":
            result = sess.run(sess.reload())
        else:
            result = f"Unknown browser action: '{action}'"

    except concurrent.futures.TimeoutError:
        result = f"Browser action '{action}' timed out (60s)."
    except Exception as e:
        err_text = str(e)
        # Dead-context detection — evict the session so the NEXT call
        # creates a fresh one instead of hitting the same stale
        # reference forever. The previous code just returned the error
        # and re-returned it on every retry: "BrowserContext has been
        # closed" became permanent.
        low = err_text.lower()
        if any(tok in low for tok in (
                "browsercontext has been closed",
                "target page, context or browser has been closed",
                "browser has been closed",
                "context has been closed")):
            try:
                target_name = _ALIASES.get(
                    (browser or _registry._active_browser
                     or _detect_default_browser()).lower().strip(),
                    (browser or _registry._active_browser
                     or _detect_default_browser()).lower().strip(),
                )
                _registry.evict(target_name)
                print(f"[Registry] Auto-evicted {target_name} after "
                      f"dead-context error on '{action}'.")
            except Exception:
                pass
            result = (f"Browser session was terminated (likely the "
                      f"automation window was closed). I have cleared "
                      f"the dead reference. Call action='attach' to "
                      f"reconnect to your running browser, or re-run "
                      f"'{action}' to open a fresh session. Underlying "
                      f"error: {err_text[:160]}")
        else:
            result = f"Browser error ({action}): {err_text}"

    _log(player, result)
    return result


def _log(player, text: str):
    short = str(text)[:80]
    print(f"[Browser] {short}")
    if player:
        player.write_log(f"[browser] {short[:60]}")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "browser_control",
    "description": "Controls any web browser like a human would — click, type, scroll, navigate, read, screenshot, hover. On macOS with Chromium-family browsers (chrome, brave, edge, vivaldi, opera, operagx) you drive the user's REAL running browser directly through two complementary bridges: (1) AppleScript for navigation, tab control, URL/title reads and JS-powered actions (get_text, click, scroll, type, fill_form) — needs Chrome's 'Allow JavaScript from Apple Events' pref enabled once (call action='enable_js' to toggle it automatically via menu), and (2) System Events keystroke/shortcut synthesis that bypasses the JS pref entirely (keystroke types into focused field; shortcut sends Cmd+L / Cmd+F / Cmd+T / etc; scroll falls back to Page-Down/Page-Up keys when JS is off). Every action attempts the JS path first and silently falls back to keystroke/HTTP-fetch when JS is blocked, so the feature works even before the user flips the Chrome pref. get_text has an HTTP-fetch fallback that reads the active URL server-side when JS is off. ALWAYS trust browser_control over vision — before you say 'no browser is open', call action='get_state' to check for real; vision only sees the frontmost window, not minimised / background browser windows. Preferences: action='set_pref' with key+value saves common contact info (name, email, phone, company, website, address, etc.) permanently; fill_form auto-fills from this store so the user never has to repeat them; pass use_prefs=false to opt out for one call. Verbs: go_to, search, back, forward, reload, new_tab, close_tab, reopen_tab, next_tab, prev_tab, list_tabs, switch_tab, get_text, get_url, get_state, click, smart_click, coord_click, hover, type, smart_type, fill_form, scroll, screenshot, press, keystroke, shortcut, focus_address, find, find_close, enable_js, set_pref, get_prefs, clear_prefs. Call 'list_browsers' to see what's open. DO NOT call 'start_debug' or 'attach' on macOS Chromium unless the user explicitly asks — recent Chrome builds disable CDP on the default profile and the relaunch just closes their browser. On Windows / Linux, or for Firefox / Safari, interactive control still needs start_debug (quits & reopens with debug port) or attach. 'attach_help' prints the manual command. fill_form proceeds silently when trust mode is on (user already granted). Always pass the 'browser' parameter when the user names one.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "go_to | search | click | coord_click | hover | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | get_state | verify_page | inspect_form | task_state | dismiss_dialogs | probe_selector | press | new_tab | close_tab | reopen_tab | next_tab | prev_tab | list_tabs | switch_tab | screenshot | back | forward | reload | keystroke | shortcut | focus_address | find | find_close | enable_js | set_pref | get_prefs | clear_prefs | trust_site | untrust_site | list_trusted_sites | attach | attach_help | start_debug | switch | list_browsers | close | close_all | trust_me"
            },
            "browser": {
                "type": "STRING",
                "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."
            },
            "url": {
                "type": "STRING",
                "description": "URL for go_to / new_tab action"
            },
            "query": {
                "type": "STRING",
                "description": "Search query for search action"
            },
            "engine": {
                "type": "STRING",
                "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"
            },
            "selector": {
                "type": "STRING",
                "description": "CSS selector for click/type"
            },
            "text": {
                "type": "STRING",
                "description": "Text to click or type"
            },
            "description": {
                "type": "STRING",
                "description": "Element description for smart_click/smart_type"
            },
            "direction": {
                "type": "STRING",
                "description": "up | down for scroll"
            },
            "amount": {
                "type": "INTEGER",
                "description": "Scroll amount in pixels (default: 500)"
            },
            "key": {
                "type": "STRING",
                "description": "Key name for press action (e.g. Enter, Escape, F5)"
            },
            "path": {
                "type": "STRING",
                "description": "Save path for screenshot"
            },
            "incognito": {
                "type": "BOOLEAN",
                "description": "Open in private/incognito mode"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Clear field before typing (default: true)"
            },
            "port": {
                "type": "INTEGER",
                "description": "CDP debug port for attach / attach_help (default: 9222)"
            },
            "index": {
                "type": "INTEGER",
                "description": "Tab index for switch_tab (0-based)"
            },
            "title_contains": {
                "type": "STRING",
                "description": "Substring of tab title or URL for switch_tab"
            },
            "mode": {
                "type": "STRING",
                "description": "For trust_me action: on | off | status"
            },
            "modifiers": {
                "type": "STRING",
                "description": "Shortcut modifiers for action='shortcut' — comma or plus-separated: 'cmd', 'cmd+shift', 'cmd,option'. Default cmd."
            },
            "value": {
                "type": "STRING",
                "description": "Value to save under 'key' for action='set_pref' (e.g. name='MD Subho', email='me@example.com')."
            },
            "use_prefs": {
                "type": "BOOLEAN",
                "description": "For fill_form: auto-merge saved preferences (default true). Set false for one-off forms."
            },
            "host": {
                "type": "STRING",
                "description": "Hostname for trust_site / untrust_site, e.g. 'rootdevs.com' or '*.example.com'. Omit to use the current active tab's host."
            },
            "which": {
                "type": "STRING",
                "description": "For inspect_form: 'primary' (first form) or a substring of a form's name/id to pick a specific one on a multi-form page."
            },
            "fields": {
                "type": "OBJECT",
                "description": "For fill_form: a dict of {field_label_or_key: value}. Values are fed to each detected input; a readback verifies them.",
                "properties": {}
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": browser_control,
}
