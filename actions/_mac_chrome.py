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
    enabled — if it isn't, a clearer AppleScriptError is raised with the
    menu path the user needs to toggle."""
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
    try:
        return _run_osascript(script)
    except AppleScriptError as e:
        msg = str(e)
        if ("Executing JavaScript through AppleScript is turned off" in msg
                or "Allow JavaScript from Apple Events" in msg):
            raise AppleScriptError(
                f"{app} needs JavaScript-from-AppleEvents enabled once: "
                f"open {app} → View menu → Developer → Allow JavaScript "
                f"from Apple Events, then try again."
            ) from e
        raise


def get_text(browser: str, limit: int = 4000) -> str:
    """Returns the visible text of the active tab, truncated.

    Primary path: `document.body.innerText` via AppleScript-run JS. If Chrome's
    'Allow JavaScript from Apple Events' pref is off (default), falls back to
    an HTTP GET of the active URL and a plain-text extraction from the HTML.
    The fallback can't see dynamic content or anything behind login, but it
    turns "I can't read this page" into a usable summary for public sites."""
    try:
        txt = execute_js(browser, "document.body.innerText")
        return txt[:limit]
    except AppleScriptError as e:
        if ("Allow JavaScript from Apple Events" not in str(e)
                and "JavaScript through AppleScript is turned off" not in str(e)):
            raise
        # Fall through to HTTP fetch.
    try:
        url = active_url(browser)
    except Exception as e2:
        raise AppleScriptError(f"could not read active URL: {e2}")
    if not url or url.startswith(("chrome://", "chrome-extension://", "about:", "file://")):
        raise AppleScriptError(
            "cannot read this tab without the JavaScript-from-AppleEvents pref "
            "(URL is browser-internal)."
        )
    return _http_fetch_text(url, limit=limit)


def _http_fetch_text(url: str, limit: int = 4000, timeout: float = 10.0) -> str:
    """Server-side read of a public web page. Not a browser — no cookies, no
    JavaScript, no DOM. Strips script/style tags and collapses whitespace.

    Uses `requests` when available because Python's bundled urllib can't find
    the system cert chain on a fresh Python install (SSL cert verify fails).
    Falls back to urllib with a verification-disabled context as a last resort
    so the feature still works for a user who hasn't run Install Certificates."""
    import re
    try:
        import requests  # type: ignore
        resp = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            allow_redirects=True,
        )
        resp.raise_for_status()
        html = resp.text
    except ImportError:
        import ssl
        import urllib.request
        ctx = ssl.create_default_context()
        try:
            import certifi  # type: ignore
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "") or ""
        enc = "utf-8"
        m = re.search(r"charset=([\w\-]+)", ctype, re.I)
        if m:
            enc = m.group(1)
        try:
            html = raw.decode(enc, errors="replace")
        except Exception:
            html = raw.decode("utf-8", errors="replace")
    # Pull <title> for context.
    title_m = re.search(r"<title[^>]*>(.*?)</title>", html,
                        flags=re.I | re.S)
    title = (title_m.group(1).strip() if title_m else "")[:200]
    # Strip <script>, <style>, <noscript>, HTML comments.
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<noscript[\s\S]*?</noscript>", " ", html, flags=re.I)
    html = re.sub(r"<!--[\s\S]*?-->", " ", html)
    # Drop tags, decode a handful of HTML entities, collapse whitespace.
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text
            .replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"\s+", " ", text).strip()
    head = f"[Title] {title}\n\n" if title else ""
    return (head + text)[:limit]


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


def activate(browser: str) -> str:
    """Launch the app if not running and bring it to the front.
    AppleScript `activate` starts the app on demand, so this doubles as
    "ensure it's open"."""
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    _run_osascript(f'tell application "{app}" to activate', timeout=15.0)
    return f"Activated {app}."


def _ensure_running(browser: str) -> None:
    """Internal: launch if not already running so subsequent calls have a
    front window to target."""
    if not is_running(browser):
        activate(browser)
        # Brief settle so a "front window" exists for the next AppleScript call.
        import time as _t
        for _ in range(20):
            if is_running(browser):
                break
            _t.sleep(0.15)


def navigate(browser: str, url: str) -> str:
    """Point the active tab at a URL. Launches the browser and opens a
    starter tab if no window is open."""
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    _ensure_running(browser)
    if not url:
        return "No URL."
    safe = url.replace('"', '%22')
    # If no window exists, `make new window with properties {URL:...}` opens
    # one; otherwise set the URL of the active tab in front window.
    script = (
        f'tell application "{app}"\n'
        f'  activate\n'
        f'  if (count of windows) = 0 then\n'
        f'    make new window with properties {{URL:"{safe}"}}\n'
        f'  else\n'
        f'    set URL of active tab of front window to "{safe}"\n'
        f'  end if\n'
        f'end tell'
    )
    _run_osascript(script, timeout=15.0)
    return f"Opened {url} in {app}."


def new_tab(browser: str, url: str = "") -> str:
    """Open a new tab in the front window (or create a window) and point it
    at `url` if given."""
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    _ensure_running(browser)
    safe = url.replace('"', '%22') if url else ""
    if safe:
        script = (
            f'tell application "{app}"\n'
            f'  activate\n'
            f'  if (count of windows) = 0 then\n'
            f'    make new window with properties {{URL:"{safe}"}}\n'
            f'  else\n'
            f'    tell front window to make new tab with properties {{URL:"{safe}"}}\n'
            f'  end if\n'
            f'end tell'
        )
    else:
        script = (
            f'tell application "{app}"\n'
            f'  activate\n'
            f'  if (count of windows) = 0 then\n'
            f'    make new window\n'
            f'  else\n'
            f'    tell front window to make new tab\n'
            f'  end if\n'
            f'end tell'
        )
    _run_osascript(script, timeout=15.0)
    return f"Opened new tab in {app}" + (f" at {url}" if url else ".")


def close_tab(browser: str) -> str:
    """Close the active tab of the front window."""
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    _run_osascript(
        f'tell application "{app}" to close active tab of front window',
        timeout=10.0,
    )
    return f"Closed tab in {app}."


def reload(browser: str) -> str:
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    _run_osascript(
        f'tell application "{app}" to reload active tab of front window',
        timeout=10.0,
    )
    return f"Reloaded {app}."


def back(browser: str) -> str:
    execute_js(browser, "window.history.back();")
    return f"Back in {browser}."


def forward(browser: str) -> str:
    execute_js(browser, "window.history.forward();")
    return f"Forward in {browser}."


def get_title(browser: str) -> str:
    return active_title(browser)


def screenshot(browser: str, path: str | None = None) -> str:
    """Capture the front browser window to a PNG via `screencapture -l`.
    Returns the saved path. No CDP needed — the OS does the compositing."""
    import os as _os
    import tempfile
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    _ensure_running(browser)
    # Look up the front window's AX window id via AppleScript.
    script = (
        f'tell application "{app}" to activate\n'
        f'delay 0.3\n'
        f'tell application "System Events" to tell process "{app}"\n'
        f'  return id of front window\n'
        f'end tell'
    )
    try:
        win_id = _run_osascript(script, timeout=10.0).strip()
    except AppleScriptError as e:
        # -1719 / accessibility denied etc.: fall back to full screen.
        win_id = ""
    out_path = path or tempfile.mktemp(prefix=f"{app.replace(' ', '_')}-", suffix=".png")
    try:
        if win_id:
            subprocess.run(
                ["screencapture", "-x", "-l", win_id, out_path],
                check=True, timeout=15.0,
            )
        else:
            subprocess.run(
                ["screencapture", "-x", out_path],
                check=True, timeout=15.0,
            )
    except subprocess.CalledProcessError as e:
        raise AppleScriptError(f"screencapture failed: {e}")
    if not _os.path.exists(out_path):
        raise AppleScriptError("screencapture produced no file")
    return f"Saved screenshot to {out_path}"


_REACT_SETTER_JS = """
window.__jarvisSetValue = function(el, v) {
  if (!el) return false;
  var tag = el.tagName;
  if (el.isContentEditable) {
    el.focus();
    try { document.execCommand('selectAll', false, null); document.execCommand('insertText', false, v); }
    catch(e){ el.textContent = v; }
  } else if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
    var proto = tag === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype
              : tag === 'SELECT'   ? window.HTMLSelectElement.prototype
              : window.HTMLInputElement.prototype;
    var desc = Object.getOwnPropertyDescriptor(proto, 'value');
    var setter = desc && desc.set;
    el.focus();
    if (setter) setter.call(el, v); else el.value = v;
  } else {
    return false;
  }
  el.dispatchEvent(new Event('input',  { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
};
window.__jarvisFind = function(hint) {
  if (!hint) return null;
  var h = String(hint).toLowerCase().trim();
  var SYN = {
    'name':     ['name','fullname','full-name','yourname','companyname','firstname','lastname','contact'],
    'email':    ['email','e-mail','mail'],
    'phone':    ['phone','mobile','tel','telephone','contactnumber'],
    'subject':  ['subject','topic','title'],
    'message':  ['message','msg','comment','comments','question','questions','inquiry','enquiry','details','description','body','content','project','feedback','note','notes','share','idea','ideas','how can we help'],
    'company':  ['company','organization','organisation','business','firm'],
    'website':  ['website','site','url','web'],
    'address':  ['address','street','city','zip','postal','country','state'],
  };
  var expanded = [h];
  for (var k in SYN) {
    if (k === h || SYN[k].indexOf(h) !== -1) {
      expanded = SYN[k].concat([k]);
      break;
    }
  }
  var fields = Array.from(document.querySelectorAll(
    'input:not([type=hidden]):not([disabled]):not([type=checkbox]):not([type=radio]):not([type=submit]):not([type=button]):not([type=reset]):not([type=file]):not([type=image]), textarea:not([disabled]), select:not([disabled]), [contenteditable=""], [contenteditable="true"]'
  )).filter(function(e){ return e.offsetParent !== null; });
  function score(el) {
    var s = 0, attrs = [];
    ['name','id','placeholder','aria-label','autocomplete','type'].forEach(function(a){
      var v = el.getAttribute && el.getAttribute(a);
      if (v) attrs.push(String(v).toLowerCase());
    });
    // Label-for linkage
    if (el.id) {
      var lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lbl && lbl.innerText) attrs.push(lbl.innerText.toLowerCase());
    }
    // Wrapping label
    var p = el.closest && el.closest('label');
    if (p && p.innerText) attrs.push(p.innerText.toLowerCase());
    // Nearby heading / text within the parent group
    var group = el.closest && el.closest('div,section,fieldset,form');
    if (group) {
      var txt = (group.innerText || '').toLowerCase().slice(0, 200);
      if (txt.includes(h)) s += 2;
    }
    for (var i = 0; i < attrs.length; i++) {
      for (var j = 0; j < expanded.length; j++) {
        if (attrs[i] === expanded[j]) { s += 10; }
        else if (attrs[i].includes(expanded[j])) { s += 5; }
      }
    }
    return s;
  }
  var best = null, bestScore = 0;
  fields.forEach(function(el){
    var s = score(el);
    if (s > bestScore) { best = el; bestScore = s; }
  });
  if (best) return best;
  // Last-resort fallbacks for free-text intents: if the hint is a "message"
  // synonym and there's exactly one textarea / contenteditable on the page,
  // use it. If it's a "name" or "email" synonym and only one matching input
  // type exists, use that. Covers simple forms where attrs are generic.
  var isMsg = (SYN['message'].indexOf(h) !== -1 || h === 'message');
  if (isMsg) {
    var tas = fields.filter(function(e){ return e.tagName === 'TEXTAREA' || e.isContentEditable; });
    if (tas.length === 1) return tas[0];
  }
  var isEmail = (SYN['email'].indexOf(h) !== -1 || h === 'email');
  if (isEmail) {
    var emails = fields.filter(function(e){ return e.type === 'email'; });
    if (emails.length === 1) return emails[0];
  }
  var isName = (SYN['name'].indexOf(h) !== -1 || h === 'name');
  if (isName) {
    var txts = fields.filter(function(e){ return e.tagName === 'INPUT' && (e.type === 'text' || e.type === ''); });
    if (txts.length === 1) return txts[0];
  }
  return null;
};
""".strip()


def type_text(browser: str, selector: str | None, text: str,
              clear_first: bool = True) -> str:
    """Type into an input/textarea/contenteditable identified by a CSS
    selector OR by a fuzzy hint (placeholder/name/label/aria-label) when the
    selector doesn't match anything. Uses React's native value-setter so
    frameworks notice the change, then fires input+change events."""
    safe_txt = (text or "").replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    safe_sel = (selector or "").replace("\\", "\\\\").replace("'", "\\'")
    clear = "true" if clear_first else "false"
    js = _REACT_SETTER_JS + f"""
    (function(){{
      var sel = '{safe_sel}';
      var el = null;
      if (sel) {{
        try {{ el = document.querySelector(sel); }} catch(e) {{ el = null; }}
        if (!el) el = window.__jarvisFind(sel);
      }} else {{
        el = document.activeElement;
        if (el === document.body) el = null;
      }}
      if (!el) return 'NOT_FOUND';
      var cur = {clear} ? '' : (el.value || el.textContent || '');
      var ok = window.__jarvisSetValue(el, cur + '{safe_txt}');
      return ok ? 'OK' : 'UNSUPPORTED';
    }})()"""
    out = execute_js(browser, js).strip()
    if out == "OK":
        return f"Typed into {browser}."
    if out == "NOT_FOUND":
        return f"Could not find a field for: {selector or '(focused element)'}"
    return f"Field cannot accept text (tag unsupported)."


def fill_form(browser: str, fields: dict) -> str:
    """Set multiple key→value pairs. Keys may be CSS selectors OR fuzzy hints
    (placeholder, name, id, label text, aria-label) — the fuzzy matcher kicks
    in when the selector doesn't find anything. Uses React-safe value
    assignment so controlled components notice the input."""
    if not fields:
        return "No fields."
    import json as _json
    payload = _json.dumps(fields)
    safe = payload.replace("\\", "\\\\").replace("'", "\\'")
    js = _REACT_SETTER_JS + f"""
    (function(){{
      var fields = JSON.parse('{safe}');
      var out = {{ok:[], miss:[]}};
      Object.keys(fields).forEach(function(key){{
        var el = null;
        try {{ el = document.querySelector(key); }} catch(e) {{}}
        if (!el) el = window.__jarvisFind(key);
        if (!el) {{ out.miss.push(key); return; }}
        if (window.__jarvisSetValue(el, String(fields[key] == null ? '' : fields[key]))) {{
          out.ok.push(key);
        }} else {{
          out.miss.push(key);
        }}
      }});
      return JSON.stringify(out);
    }})()"""
    raw = execute_js(browser, js)
    try:
        res = __import__("json").loads(raw)
        parts = [f"filled {len(res.get('ok', []))}"]
        if res.get("miss"):
            parts.append(f"missed {', '.join(res['miss'])}")
        return "; ".join(parts) + f" in {browser}."
    except Exception:
        return f"fill_form response: {raw[:120]}"


# ── Human-like control via System Events ─────────────────────────────────────
# These bypass Chrome's "Allow JavaScript from Apple Events" pref entirely by
# synthesising real OS-level keyboard events against the focused browser
# window. Covers scrolling, typing into focused fields, address-bar navigation,
# tab management shortcuts, find-in-page, developer tools, etc. Requires
# Accessibility permission for the controlling process (granted once per app
# in System Settings → Privacy & Security → Accessibility).

_MOD_MAP = {
    "cmd":     "command down",
    "command": "command down",
    "shift":   "shift down",
    "opt":     "option down",
    "option":  "option down",
    "alt":     "option down",
    "ctrl":    "control down",
    "control": "control down",
    "fn":      "function down",
}

_KEYCODE_MAP = {
    # macOS virtual key codes — needed for keys that AppleScript can't address
    # by character (arrows, function keys, Return, Tab, etc.).
    "return":    "36",
    "enter":     "36",
    "tab":       "48",
    "space":     "49",
    "delete":    "51",
    "backspace": "51",
    "esc":       "53",
    "escape":    "53",
    "left":      "123",
    "right":     "124",
    "down":      "125",
    "up":        "126",
    "pageup":    "116",
    "pagedown":  "121",
    "home":      "115",
    "end":       "119",
    "f1":  "122", "f2":  "120", "f3":  "99",  "f4":  "118",
    "f5":  "96",  "f6":  "97",  "f7":  "98",  "f8":  "100",
    "f9":  "101", "f10": "109", "f11": "103", "f12": "111",
}


def _focus_browser(browser: str) -> str:
    """Bring the browser to the front so keystrokes land in it. Returns the
    app name (used by subsequent `tell process` blocks)."""
    app = app_name(browser)
    if not app:
        raise AppleScriptError(f"Unsupported browser: {browser}")
    _ensure_running(browser)
    _run_osascript(f'tell application "{app}" to activate', timeout=10.0)
    import time as _t
    _t.sleep(0.15)  # settle front-window focus
    return app


def shortcut(browser: str, key: str, modifiers=("cmd",)) -> str:
    """Send a keyboard shortcut to the browser. `key` is a single character
    (e.g. 'l' for Cmd+L) or a named key from _KEYCODE_MAP. `modifiers` is a
    tuple/list of 'cmd','shift','opt','ctrl','fn'."""
    app = _focus_browser(browser)
    mods = ", ".join(_MOD_MAP[m.lower()] for m in modifiers if m.lower() in _MOD_MAP)
    using = f" using {{{mods}}}" if mods else ""
    k = (key or "").lower()
    if k in _KEYCODE_MAP:
        action = f"key code {_KEYCODE_MAP[k]}"
    else:
        ch = (key or "").replace("\\", "\\\\").replace('"', '\\"')
        action = f'keystroke "{ch}"'
    script = (
        f'tell application "System Events" to tell process "{app}"\n'
        f'  {action}{using}\n'
        f'end tell'
    )
    _run_osascript(script, timeout=10.0)
    label = "+".join(list(modifiers) + [key])
    return f"Pressed {label} in {app}."


def keystroke(browser: str, text: str) -> str:
    """Type text into the currently focused field via real OS keystrokes.
    Works without any Chrome preference — does not touch JS."""
    app = _focus_browser(browser)
    safe = (text or "").replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'tell application "System Events" to tell process "{app}"\n'
        f'  keystroke "{safe}"\n'
        f'end tell'
    )
    _run_osascript(script, timeout=max(10.0, len(text) * 0.05))
    return f"Typed {len(text)} characters into {app}."


def go_to_via_address_bar(browser: str, url: str) -> str:
    """Cmd+L, type URL, Return. Works without any Chrome JS pref and lands in
    the active tab the way a human would navigate."""
    app = _focus_browser(browser)
    safe = (url or "").replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'tell application "System Events" to tell process "{app}"\n'
        f'  keystroke "l" using {{command down}}\n'
        f'  delay 0.15\n'
        f'  keystroke "{safe}"\n'
        f'  delay 0.1\n'
        f'  key code 36\n'
        f'end tell'
    )
    _run_osascript(script, timeout=20.0)
    return f"Navigated to {url} in {app}."


def scroll_keys(browser: str, direction: str = "down", pages: int = 1) -> str:
    """Scroll via Page Down / Page Up / arrow keys instead of JS. Good for
    reading long pages when JS-from-AppleEvents is off."""
    app = _focus_browser(browser)
    direction = (direction or "down").lower()
    kc = {"down": 121, "up": 116, "home": 115, "end": 119,
          "left": 123, "right": 124}.get(direction, 121)
    pages = max(1, int(pages))
    script = (
        f'tell application "System Events" to tell process "{app}"\n'
        f'  repeat {pages} times\n'
        f'    key code {kc}\n'
        f'    delay 0.08\n'
        f'  end repeat\n'
        f'end tell'
    )
    _run_osascript(script, timeout=10.0 + pages * 0.5)
    return f"Scrolled {direction} ×{pages} in {app}."


def find_in_page(browser: str, text: str) -> str:
    """Cmd+F, type text, Return. Highlights all matches and jumps to first."""
    app = _focus_browser(browser)
    safe = (text or "").replace("\\", "\\\\").replace('"', '\\"')
    if not safe:
        return "Empty find text."
    script = (
        f'tell application "System Events" to tell process "{app}"\n'
        f'  keystroke "f" using {{command down}}\n'
        f'  delay 0.2\n'
        f'  keystroke "{safe}"\n'
        f'  delay 0.1\n'
        f'  key code 36\n'
        f'end tell'
    )
    _run_osascript(script, timeout=12.0)
    return f"Finding '{text}' in {app}."


def close_find(browser: str) -> str:
    app = _focus_browser(browser)
    _run_osascript(
        f'tell application "System Events" to tell process "{app}" to key code 53',
        timeout=8.0,
    )
    return f"Closed find bar in {app}."


def new_tab_shortcut(browser: str) -> str:
    return shortcut(browser, "t", ("cmd",))


def close_tab_shortcut(browser: str) -> str:
    return shortcut(browser, "w", ("cmd",))


def reopen_tab(browser: str) -> str:
    return shortcut(browser, "t", ("cmd", "shift"))


def next_tab(browser: str) -> str:
    return shortcut(browser, "right", ("cmd", "option"))


def prev_tab(browser: str) -> str:
    return shortcut(browser, "left", ("cmd", "option"))


def enable_js_from_apple_events(browser: str) -> str:
    """Toggle Chrome's 'Allow JavaScript from Apple Events' pref via its own
    menu item, using System Events. One-time setup so every JS-based action
    (get_text, click_text, scroll, type, fill_form) lights up without the user
    having to touch the menu themselves.

    Requires Accessibility permission for the controlling process. If that's
    not granted, the AppleScript error (-1719 or -25211) surfaces unchanged so
    the caller can tell the user where to enable it.
    """
    app = _focus_browser(browser)
    # Check the current state before clicking; if it's already on, don't toggle
    # it off. AppleScript exposes a boolean attribute `value` on menu items
    # that act like checkboxes.
    probe = (
        f'tell application "System Events" to tell process "{app}"\n'
        f'  try\n'
        f'    tell menu bar 1\n'
        f'      tell menu bar item "View"\n'
        f'        tell menu 1\n'
        f'          tell menu item "Developer"\n'
        f'            tell menu 1\n'
        f'              set v to value of attribute "AXMenuItemMarkChar" of menu item "Allow JavaScript from Apple Events"\n'
        f'              return v as text\n'
        f'            end tell\n'
        f'          end tell\n'
        f'        end tell\n'
        f'      end tell\n'
        f'    end tell\n'
        f'  on error errMsg\n'
        f'    return "ERR: " & errMsg\n'
        f'  end try\n'
        f'end tell'
    )
    try:
        state = _run_osascript(probe, timeout=10.0).strip()
    except AppleScriptError as e:
        return (f"Could not read the View → Developer menu in {app}: {e}. "
                f"Grant Accessibility permission to your terminal/Python in "
                f"System Settings → Privacy & Security → Accessibility.")
    # AXMenuItemMarkChar is "✓" (or similar) when checked, "" when unchecked.
    if state and state.startswith("ERR:"):
        return (f"Menu probe failed: {state[5:].strip()}. The menu item may "
                f"exist under a different name in your {app} version.")
    already_on = bool(state) and state not in ("", "missing value")
    if already_on:
        return f"'Allow JavaScript from Apple Events' is already enabled in {app}."
    click = (
        f'tell application "System Events" to tell process "{app}"\n'
        f'  tell menu bar 1\n'
        f'    tell menu bar item "View"\n'
        f'      tell menu 1\n'
        f'        tell menu item "Developer"\n'
        f'          tell menu 1\n'
        f'            click menu item "Allow JavaScript from Apple Events"\n'
        f'          end tell\n'
        f'        end tell\n'
        f'      end tell\n'
        f'    end tell\n'
        f'  end tell\n'
        f'end tell'
    )
    _run_osascript(click, timeout=10.0)
    return (f"Enabled 'Allow JavaScript from Apple Events' in {app}. "
            f"Every browser action is now unrestricted.")


def wait_for(browser: str, selector: str, timeout_s: float = 10.0) -> str:
    """Poll the page for a selector via JS. Returns OK or TIMEOUT."""
    import time as _t
    safe = selector.replace("\\", "\\\\").replace("'", "\\'")
    deadline = _t.monotonic() + max(1.0, float(timeout_s))
    while _t.monotonic() < deadline:
        out = execute_js(
            browser,
            f"document.querySelector('{safe}') ? 'OK' : 'MISS'",
        ).strip()
        if out == "OK":
            return f"Found: {selector}"
        _t.sleep(0.4)
    return f"Timed out waiting for: {selector}"


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
