"""
AppleScript bridge for macOS native apps: Calendar, Contacts, Reminders, Mail.

Mirrors the design of `_mac_chrome.py` — small, defensive, raises
`AppleScriptError` on failure so callers can degrade cleanly. Each helper
runs one osascript subprocess; no event stream, no permanent connection.

Permission model: macOS asks the controlling process ONCE per target app
for Automation access the first time any script addresses that app. The
user grants or denies in System Settings → Privacy & Security → Automation.
A denial surfaces as error `-1743` which callers can key on.
"""
from __future__ import annotations

import datetime as _dt
import json
import platform
import subprocess
from typing import Any


class AppleScriptError(RuntimeError):
    pass


def available() -> bool:
    return platform.system() == "Darwin"


def _run(script: str, timeout: float = 15.0) -> str:
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


def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def _is_running(app: str) -> bool:
    try:
        res = subprocess.run(
            ["pgrep", "-x", app], capture_output=True, text=True, timeout=3,
        )
        return res.returncode == 0
    except Exception:
        return False


# ── Calendar ─────────────────────────────────────────────────────────────────

def _parse_dt(s: str) -> _dt.datetime:
    """Accept common forms: 2026-11-10 14:00, 2026-11-10T14:00, 2026-11-10,
    'tomorrow 3pm' NOT supported here — the model should resolve those first."""
    s = (s or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d",
                "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise AppleScriptError(f"could not parse datetime: {s!r}")


def _as_date(dt: _dt.datetime) -> str:
    """Build an AppleScript `date` expression from a datetime, independent
    of macOS locale (which breaks `date "2026-11-10 14:00"` outside en_US)."""
    return (
        f'(current date) '
        f'as date\n'
        f'set theDate to it\n'
        f'set year of theDate to {dt.year}\n'
        f'set month of theDate to {dt.month}\n'
        f'set day of theDate to {dt.day}\n'
        f'set hours of theDate to {dt.hour}\n'
        f'set minutes of theDate to {dt.minute}\n'
        f'set seconds of theDate to {dt.second}'
    )


def calendar_create_event(title: str, start: str, end: str | None = None,
                          duration_min: int = 60,
                          calendar_name: str | None = None,
                          location: str = "", notes: str = "") -> str:
    s_dt = _parse_dt(start)
    e_dt = _parse_dt(end) if end else s_dt + _dt.timedelta(minutes=duration_min)
    cal = _esc(calendar_name) if calendar_name else ""
    script = (
        'tell application "Calendar"\n'
        '  set startD to current date\n'
        f'  set year of startD to {s_dt.year}\n'
        f'  set month of startD to {s_dt.month}\n'
        f'  set day of startD to {s_dt.day}\n'
        f'  set hours of startD to {s_dt.hour}\n'
        f'  set minutes of startD to {s_dt.minute}\n'
        '  set seconds of startD to 0\n'
        '  set endD to current date\n'
        f'  set year of endD to {e_dt.year}\n'
        f'  set month of endD to {e_dt.month}\n'
        f'  set day of endD to {e_dt.day}\n'
        f'  set hours of endD to {e_dt.hour}\n'
        f'  set minutes of endD to {e_dt.minute}\n'
        '  set seconds of endD to 0\n'
        + (f'  set theCal to first calendar whose name is "{cal}"\n'
           if cal else '  set theCal to first calendar\n')
        + f'  tell theCal to make new event with properties {{summary:"{_esc(title)}", '
          f'start date:startD, end date:endD, '
          f'location:"{_esc(location)}", description:"{_esc(notes)}"}}\n'
        'end tell\n'
        'return "ok"'
    )
    _run(script, timeout=20.0)
    return (f"Added to Calendar: {title} on "
            f"{s_dt:%Y-%m-%d %H:%M} ({(e_dt - s_dt).seconds // 60} min).")


def calendar_list_events(days: int = 7, calendar_name: str | None = None) -> list[dict]:
    """Return events in the next `days` days across the given calendar (or
    all calendars). Fields: title, start, end, location, calendar."""
    now = _dt.datetime.now()
    end = now + _dt.timedelta(days=max(1, days))
    cal_pred = (f'name of theCal is "{_esc(calendar_name)}"'
                if calendar_name else "true")
    script = (
        'set out to {}\n'
        'tell application "Calendar"\n'
        '  set startD to current date\n'
        f'  set year of startD to {now.year}\n'
        f'  set month of startD to {now.month}\n'
        f'  set day of startD to {now.day}\n'
        '  set hours of startD to 0\n  set minutes of startD to 0\n  set seconds of startD to 0\n'
        '  set endD to current date\n'
        f'  set year of endD to {end.year}\n'
        f'  set month of endD to {end.month}\n'
        f'  set day of endD to {end.day}\n'
        '  set hours of endD to 23\n  set minutes of endD to 59\n  set seconds of endD to 59\n'
        '  repeat with theCal in calendars\n'
        f'    if {cal_pred} then\n'
        '      repeat with ev in (every event of theCal whose start date >= startD and start date <= endD)\n'
        '        set end of out to (summary of ev) & "|" & ((start date of ev) as string) & "|" & ((end date of ev) as string) & "|" & (location of ev) & "|" & (name of theCal)\n'
        '      end repeat\n'
        '    end if\n'
        '  end repeat\n'
        'end tell\n'
        'set AppleScript\'s text item delimiters to linefeed\n'
        'set txt to out as text\n'
        'set AppleScript\'s text item delimiters to ""\n'
        'return txt'
    )
    raw = _run(script, timeout=25.0)
    events: list[dict] = []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        events.append({
            "title":    parts[0],
            "start":    parts[1],
            "end":      parts[2],
            "location": parts[3],
            "calendar": parts[4],
        })
    return events


# ── Contacts ─────────────────────────────────────────────────────────────────

def contacts_search(query: str, limit: int = 10) -> list[dict]:
    """Search people by name. Returns list of {name, emails, phones}."""
    q = _esc(query)
    script = (
        'set out to {}\n'
        'tell application "Contacts"\n'
        f'  set matches to (every person whose name contains "{q}")\n'
        '  repeat with p in matches\n'
        '    set em to ""\n'
        '    repeat with e in emails of p\n'
        '      if em is "" then set em to (value of e) else set em to em & "," & (value of e)\n'
        '    end repeat\n'
        '    set ph to ""\n'
        '    repeat with pn in phones of p\n'
        '      if ph is "" then set ph to (value of pn) else set ph to ph & "," & (value of pn)\n'
        '    end repeat\n'
        '    set end of out to (name of p) & "||" & em & "||" & ph\n'
        '  end repeat\n'
        'end tell\n'
        'set AppleScript\'s text item delimiters to linefeed\n'
        'set txt to out as text\n'
        'set AppleScript\'s text item delimiters to ""\n'
        'return txt'
    )
    raw = _run(script, timeout=20.0)
    out: list[dict] = []
    for line in raw.splitlines():
        parts = line.split("||")
        if len(parts) < 3:
            continue
        out.append({
            "name":   parts[0],
            "emails": [e for e in parts[1].split(",") if e],
            "phones": [p for p in parts[2].split(",") if p],
        })
        if len(out) >= limit:
            break
    return out


# ── Reminders (Apple Reminders.app) ─────────────────────────────────────────

def reminders_add(text: str, due: str | None = None,
                  list_name: str | None = None) -> str:
    """Create a reminder in Apple Reminders. `due` accepted as YYYY-MM-DD HH:MM."""
    due_block = ""
    if due:
        d = _parse_dt(due)
        due_block = (
            '  set dD to current date\n'
            f'  set year of dD to {d.year}\n'
            f'  set month of dD to {d.month}\n'
            f'  set day of dD to {d.day}\n'
            f'  set hours of dD to {d.hour}\n'
            f'  set minutes of dD to {d.minute}\n'
            '  set seconds of dD to 0\n'
        )
    if list_name:
        tell = (f'  set lst to first list whose name is "{_esc(list_name)}"\n'
                '  tell lst\n')
    else:
        tell = '  set lst to default list\n  tell lst\n'
    script = (
        'tell application "Reminders"\n'
        + tell + due_block +
        f'    make new reminder with properties {{name:"{_esc(text)}"' +
        (", due date:dD" if due else "") +
        '}}\n'
        '  end tell\n'
        'end tell\n'
        'return "ok"'
    )
    _run(script, timeout=15.0)
    return f"Reminder added: {text}" + (f" (due {due})" if due else "")


def reminders_list(list_name: str | None = None, include_done: bool = False) -> list[dict]:
    pred = "true" if include_done else "completed is false"
    if list_name:
        source = (f'set lst to first list whose name is "{_esc(list_name)}"\n'
                  'tell lst\n')
    else:
        source = 'repeat with lst in lists\n  tell lst\n'
    end = 'end tell' + ('' if list_name else '\nend repeat')
    script = (
        'set out to {}\n'
        'tell application "Reminders"\n' + source +
        f'  repeat with r in (every reminder whose {pred})\n'
        '    set due to ""\n'
        '    try\n      set due to (due date of r) as string\n    end try\n'
        '    set end of out to (name of r) & "|" & due & "|" & (name of lst)\n'
        '  end repeat\n' + end + '\nend tell\n'
        'set AppleScript\'s text item delimiters to linefeed\n'
        'set txt to out as text\n'
        'set AppleScript\'s text item delimiters to ""\n'
        'return txt'
    )
    raw = _run(script, timeout=20.0)
    out: list[dict] = []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        out.append({"text": parts[0], "due": parts[1], "list": parts[2]})
    return out


# ── Mail ─────────────────────────────────────────────────────────────────────

def mail_list_recent(mailbox: str = "INBOX", limit: int = 10,
                     unread_only: bool = False) -> list[dict]:
    """Recent messages from Mail.app. Fields: id, from, subject, date, unread."""
    if unread_only:
        pred = "read status is false"
    else:
        pred = "true"
    script = (
        'set out to {}\n'
        'tell application "Mail"\n'
        f'  set boxes to (every mailbox whose name is "{_esc(mailbox)}")\n'
        '  repeat with mb in boxes\n'
        f'    set msgs to (messages of mb whose {pred})\n'
        f'    set msgCount to count of msgs\n'
        f'    set upper to msgCount\n'
        f'    set lower to upper - ({max(1, limit)} - 1)\n'
        '    if lower < 1 then set lower to 1\n'
        '    repeat with i from upper to lower by -1\n'
        '      set m to item i of msgs\n'
        '      set end of out to ((id of m) as string) & "|" & (sender of m) & "|" & (subject of m) & "|" & ((date received of m) as string) & "|" & ((read status of m) as string)\n'
        '    end repeat\n'
        '  end repeat\n'
        'end tell\n'
        'set AppleScript\'s text item delimiters to linefeed\n'
        'set txt to out as text\n'
        'set AppleScript\'s text item delimiters to ""\n'
        'return txt'
    )
    raw = _run(script, timeout=25.0)
    out: list[dict] = []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        out.append({
            "id":      parts[0],
            "from":    parts[1],
            "subject": parts[2],
            "date":    parts[3],
            "unread":  parts[4].strip().lower() == "false",
        })
    return out


def mail_read(message_id: str) -> dict:
    mid = _esc(message_id)
    script = (
        'tell application "Mail"\n'
        f'  set m to (first message whose id is "{mid}")\n'
        '  return (subject of m) & "\\n---\\n" & (sender of m) & "\\n---\\n" & ((content of m) as text)\n'
        'end tell'
    )
    raw = _run(script, timeout=20.0)
    parts = raw.split("\n---\n", 2)
    if len(parts) < 3:
        return {"subject": "", "from": "", "body": raw}
    return {"subject": parts[0], "from": parts[1], "body": parts[2]}


def mail_compose(to: str, subject: str, body: str,
                 cc: str = "", send: bool = False) -> str:
    """Build a Mail.app message. `send=True` actually sends; otherwise it
    opens for the user to review. Sending always requires explicit intent."""
    script = (
        'tell application "Mail"\n'
        f'  set msg to make new outgoing message with properties {{subject:"{_esc(subject)}", content:"{_esc(body)}", visible:true}}\n'
        '  tell msg\n'
        f'    make new to recipient at end of to recipients with properties {{address:"{_esc(to)}"}}\n'
        + (f'    make new cc recipient at end of cc recipients with properties {{address:"{_esc(cc)}"}}\n'
           if cc else "")
        + ('    send\n' if send else '')
        + '  end tell\n'
        'end tell\n'
        'return "ok"'
    )
    _run(script, timeout=25.0)
    return (f"Email {'sent' if send else 'drafted'} to {to}: {subject}")
