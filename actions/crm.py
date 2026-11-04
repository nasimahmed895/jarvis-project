"""
CRM action — user-facing contact / stakeholder / follow-up management.

Thin wrapper over ``core/crm.py`` so the SQLite work lives next to the
other agent memory layers (tasks, journal, vectors) and the tool surface
exposed to the model stays small and declarative. Complements
``actions/contacts_manager.py``, which stays read-only against the macOS
address book — this module owns JARVIS's private CRM view: VIP tiers,
interaction log, follow-up queue.

Everything is a local disk write. No permission gate.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from core import crm as _crm


_CONNECTED = False


def _ensure() -> None:
    global _CONNECTED
    if _CONNECTED:
        return
    db = Path(__file__).resolve().parent.parent / "memory" / "crm.sqlite"
    _crm.connect(db)
    _CONNECTED = True


def _parse_due(text: str | None) -> float | None:
    """Accept YYYY-MM-DD, YYYY-MM-DD HH:MM, or an epoch seconds number. Loose
    parser on purpose — the planner generates these, and a stricter one just
    pushes failures onto the retry loop. Returns None when parsing fails."""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip()
    try:
        return float(s)
    except ValueError:
        pass
    import datetime as _dt
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d",
                "%d-%m-%Y %H:%M", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return _dt.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def _fmt_contact(c: dict) -> str:
    bits = [f"#{c['id']} [{c['tier']}] {c['name']}"]
    for k in ("email", "phone", "org", "role"):
        v = c.get(k)
        if v:
            bits.append(f"{k}={v}")
    if c.get("last_seen"):
        days = (time.time() - float(c["last_seen"])) / 86400.0
        bits.append(f"last_seen={days:.1f}d ago")
    return " · ".join(bits)


def _fmt_follow(f: dict) -> str:
    who = f.get("contact_name") or "(no contact)"
    tier = f.get("contact_tier") or ""
    due = f.get("due_ts")
    if due:
        from datetime import datetime as _dt
        due_str = _dt.fromtimestamp(float(due)).strftime("%Y-%m-%d %H:%M")
    else:
        due_str = "no date"
    tag = f"[{tier}] " if tier else ""
    return f"#{f['id']} {tag}{who} — {f['text']} (due {due_str})"


def crm(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Manage private CRM: contacts, tiers (VIP), interactions, follow-ups.

    Actions:
      add_contact    name= email= phone= org= role= tier= birthday= notes=
      update_contact id= [any of the above fields]
      set_tier       id= tier=<vip|client|investor|colleague|family|vendor|regular>
      delete_contact id=
      list_contacts  tier= (optional)
      search         query=
      log            id= kind= summary= subject=  — record an interaction
      recent         id=     — recent interactions for one contact
      add_follow_up  text= id=(contact) due=YYYY-MM-DD
      due_follow_ups within_hours= (default 24)
      resolve        id=(follow-up)
      list_follow_ups status=<open|done> (default open)
      stats          — counts of contacts / interactions / open follow-ups
    """
    _ensure()
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()

    if action == "add_contact":
        name = str(params.get("name") or "").strip()
        if not name:
            return "crm add_contact: provide 'name'."
        try:
            cid = _crm.add_contact(
                name=name,
                email=params.get("email") or None,
                phone=params.get("phone") or None,
                org=params.get("org") or None,
                role=params.get("role") or None,
                tier=str(params.get("tier") or "regular"),
                birthday=params.get("birthday") or None,
                notes=params.get("notes") or None,
            )
        except ValueError as e:
            return f"crm add_contact: {e}"
        return f"Added contact #{cid}: {name}"

    if action == "update_contact":
        try:
            cid = int(params.get("id"))
        except (TypeError, ValueError):
            return "crm update_contact: provide numeric 'id'."
        patch: dict = {}
        for k in ("name", "tier", "email", "phone", "org", "role",
                  "notes", "birthday"):
            if k in params and params[k] is not None:
                patch[k] = params[k]
        if not patch:
            return "crm update_contact: nothing to update."
        if _crm.update_contact(cid, **patch):
            return f"Updated contact #{cid}."
        return f"Contact #{cid} not found."

    if action == "set_tier":
        try:
            cid = int(params.get("id"))
        except (TypeError, ValueError):
            return "crm set_tier: provide numeric 'id'."
        tier = str(params.get("tier") or "").lower()
        if tier not in _crm.TIERS:
            return (f"crm set_tier: tier must be one of "
                    f"{', '.join(_crm.TIERS)}.")
        if _crm.set_tier(cid, tier):
            return f"Contact #{cid} tier set to {tier}."
        return f"Contact #{cid} not found."

    if action == "delete_contact":
        try:
            cid = int(params.get("id"))
        except (TypeError, ValueError):
            return "crm delete_contact: provide numeric 'id'."
        if _crm.delete_contact(cid):
            return f"Deleted contact #{cid}."
        return f"Contact #{cid} not found."

    if action == "list_contacts":
        tier = params.get("tier")
        rows = _crm.list_contacts(tier=tier)
        if not rows:
            return "No contacts."
        head = f"Contacts ({len(rows)}):"
        return head + "\n" + "\n".join("  " + _fmt_contact(r) for r in rows)

    if action == "search":
        q = str(params.get("query") or "").strip()
        rows = _crm.search_contacts(q)
        if not rows:
            return f"No contact matches for {q!r}."
        return (f"Matches for {q!r}:\n"
                + "\n".join("  " + _fmt_contact(r) for r in rows))

    if action == "log":
        try:
            cid = int(params.get("id"))
        except (TypeError, ValueError):
            return "crm log: provide numeric 'id' (contact)."
        kind = str(params.get("kind") or "note").strip() or "note"
        iid = _crm.log_interaction(
            cid, kind,
            summary=params.get("summary") or None,
            subject=params.get("subject") or None,
        )
        return f"Logged interaction #{iid} ({kind}) for contact #{cid}."

    if action == "recent":
        try:
            cid = int(params.get("id"))
        except (TypeError, ValueError):
            return "crm recent: provide numeric 'id' (contact)."
        rows = _crm.recent_interactions(cid)
        if not rows:
            return f"No interactions for contact #{cid}."
        from datetime import datetime as _dt
        lines = [f"Recent interactions for contact #{cid}:"]
        for r in rows:
            when = _dt.fromtimestamp(float(r["ts"])).strftime("%Y-%m-%d")
            subj = r.get("subject") or ""
            summ = r.get("summary") or ""
            lines.append(f"  {when} [{r['kind']}] {subj} {summ}".rstrip())
        return "\n".join(lines)

    if action == "add_follow_up":
        text = str(params.get("text") or "").strip()
        if not text:
            return "crm add_follow_up: provide 'text'."
        cid = params.get("id")
        try:
            cid = int(cid) if cid is not None else None
        except (TypeError, ValueError):
            cid = None
        due_ts = _parse_due(params.get("due"))
        fid = _crm.add_follow_up(text, contact_id=cid, due_ts=due_ts)
        return f"Added follow-up #{fid}: {text}"

    if action == "due_follow_ups":
        try:
            hours = float(params.get("within_hours", 24))
        except (TypeError, ValueError):
            hours = 24.0
        rows = _crm.due_follow_ups(within_hours=hours)
        if not rows:
            return f"No follow-ups due within {hours}h."
        return (f"Due follow-ups within {hours:g}h ({len(rows)}):\n"
                + "\n".join("  " + _fmt_follow(r) for r in rows))

    if action == "resolve":
        try:
            fid = int(params.get("id"))
        except (TypeError, ValueError):
            return "crm resolve: provide numeric 'id' (follow-up)."
        if _crm.resolve_follow_up(fid):
            return f"Resolved follow-up #{fid}."
        return f"Follow-up #{fid} not found or already resolved."

    if action == "list_follow_ups":
        status = str(params.get("status") or "open").lower()
        rows = _crm.list_follow_ups(status=status)
        if not rows:
            return f"No follow-ups with status {status!r}."
        return (f"Follow-ups ({status}, {len(rows)}):\n"
                + "\n".join("  " + _fmt_follow(r) for r in rows))

    if action == "stats":
        s = _crm.stats()
        parts = [f"Contacts by tier: {s['contacts_by_tier']}",
                 f"Interactions logged: {s['interactions']}",
                 f"Open follow-ups: {s['open_follow_ups']}"]
        return "\n".join(parts)

    return (f"crm: unknown action {action!r}. "
            "Actions: add_contact | update_contact | set_tier | "
            "delete_contact | list_contacts | search | log | recent | "
            "add_follow_up | due_follow_ups | resolve | list_follow_ups | "
            "stats")


TOOL = {
    "name": "crm",
    "description": (
        "Private CRM for executive-assistant work. Use this to tag "
        "important people (VIPs, clients, investors, family, colleagues), "
        "log interactions, and queue follow-ups. Does NOT modify the "
        "system address book. Add people here the moment the user names "
        "them with a role ('my investor Sarah', 'my client at Acme', "
        "'my sister Fatma') — the gatekeeper needs tier data to decide "
        "whether an email is urgent. Tiers: vip, client, investor, "
        "colleague, family, vendor, regular. Call 'log' after a meeting "
        "or email so 'when did I last talk to X?' has a real answer. "
        "Call 'add_follow_up' when the user promises to get back to "
        "someone — then 'due_follow_ups' every morning to see what is "
        "owed. Call 'search' before 'add_contact' so you don't create "
        "duplicates."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":       {"type": "STRING",  "description": (
                "add_contact | update_contact | set_tier | delete_contact "
                "| list_contacts | search | log | recent | add_follow_up "
                "| due_follow_ups | resolve | list_follow_ups | stats")},
            "id":           {"type": "INTEGER", "description": "Contact or follow-up id."},
            "name":         {"type": "STRING",  "description": "Contact name."},
            "email":        {"type": "STRING",  "description": "Contact email."},
            "phone":        {"type": "STRING",  "description": "Phone number."},
            "org":          {"type": "STRING",  "description": "Organisation / company."},
            "role":         {"type": "STRING",  "description": "Role or title."},
            "tier":         {"type": "STRING",  "description": "vip | client | investor | colleague | family | vendor | regular"},
            "birthday":     {"type": "STRING",  "description": "Birthday YYYY-MM-DD."},
            "notes":        {"type": "STRING",  "description": "Free-text notes."},
            "query":        {"type": "STRING",  "description": "Substring for 'search'."},
            "kind":         {"type": "STRING",  "description": "Interaction kind for 'log' (email_in | email_out | meeting | call | message_in | message_out | note)."},
            "summary":      {"type": "STRING",  "description": "Short summary of the interaction."},
            "subject":      {"type": "STRING",  "description": "Subject line (emails/meetings)."},
            "text":         {"type": "STRING",  "description": "Follow-up text."},
            "due":          {"type": "STRING",  "description": "Follow-up due date YYYY-MM-DD or YYYY-MM-DD HH:MM."},
            "within_hours": {"type": "NUMBER",  "description": "Horizon for 'due_follow_ups' in hours (default 24)."},
            "status":       {"type": "STRING",  "description": "open | done (for list_follow_ups)."},
        },
        "required": ["action"],
    },
    "handler": crm,
}
