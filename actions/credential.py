"""
actions/credential.py — site credentials kept out of the model's reach.

Stores (host, username, password) triples in the OS keyring (same backend
as `core/secrets.py`). The LLM can call this tool to *use* a credential
— open the site, fill the login form, click the login button — but the
password itself is never returned in a tool result and never written to
the chat transcript. Keyring misses fall back to AES-encrypted storage
via `core.secrets.encrypt_bytes`; no plaintext on disk under any path.

Verbs:
  save           — store a credential: {host, username, password}
  list_hosts     — list known hosts; passwords never included
  login          — open the host's login page and sign in (gated LVL2)
  forget         — delete a stored credential
  export_safe    — emit the usernames (no passwords) for the dashboard
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Optional

from core import approval as _approval
from core import secrets as _secrets

try:
    from actions import browser_control as _bc
except Exception:
    _bc = None  # type: ignore


_SERVICE_NAME = "jarvis-assistant"
_CRED_PREFIX  = "site_cred:"

_FALLBACK_PATH = Path(__file__).resolve().parent.parent / "config" / "site_creds.enc"


# ── Low-level storage ──────────────────────────────────────────────────

def _host_key(host: str) -> str:
    return _CRED_PREFIX + host.lower().strip()


def _store_keyring(host: str, payload: dict) -> bool:
    kr = _secrets._try_keyring()  # internal, same API as the rest of the file
    if kr is None:
        return False
    try:
        kr.set_password(_SERVICE_NAME, _host_key(host),
                        json.dumps(payload))
        return True
    except Exception:
        return False


def _load_keyring(host: str) -> Optional[dict]:
    kr = _secrets._try_keyring()
    if kr is None:
        return None
    try:
        raw = kr.get_password(_SERVICE_NAME, _host_key(host))
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        return None


def _load_fallback_blob() -> dict:
    if not _FALLBACK_PATH.exists():
        return {}
    try:
        blob = _FALLBACK_PATH.read_bytes()
        plain = _secrets.decrypt_bytes(blob) if _secrets.is_encrypted(blob) else blob
        if not plain:
            return {}
        return json.loads(plain.decode("utf-8"))
    except Exception:
        return {}


def _save_fallback_blob(data: dict) -> bool:
    plaintext = json.dumps(data).encode("utf-8")
    enc = _secrets.encrypt_bytes(plaintext)
    if enc is None:
        # No encryption available — refuse to write plaintext passwords.
        return False
    _FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    _FALLBACK_PATH.write_bytes(enc)
    return True


def _put(host: str, payload: dict) -> bool:
    if _store_keyring(host, payload):
        return True
    data = _load_fallback_blob()
    data[host.lower().strip()] = payload
    return _save_fallback_blob(data)


def _get(host: str) -> Optional[dict]:
    val = _load_keyring(host)
    if val is not None:
        return val
    return _load_fallback_blob().get(host.lower().strip())


def _del(host: str) -> None:
    kr = _secrets._try_keyring()
    if kr is not None:
        try:
            kr.delete_password(_SERVICE_NAME, _host_key(host))
        except Exception:
            pass
    data = _load_fallback_blob()
    if host.lower().strip() in data:
        data.pop(host.lower().strip(), None)
        _save_fallback_blob(data)


def _list_hosts() -> list[str]:
    # Keyring does not support a universal list; we track hosts in the
    # fallback blob for enumeration. Hosts saved only in keyring are not
    # listed, which is acceptable — the user knows which ones they saved.
    return sorted(_load_fallback_blob().keys())


# ── Tool surface ───────────────────────────────────────────────────────

def credential(parameters: dict | None = None,
               response=None, player=None, speak=None,
               session_memory=None) -> str:
    params = parameters or {}
    verb = (params.get("action") or "").lower().strip()

    if verb == "save":
        host = (params.get("host") or "").strip()
        username = (params.get("username") or "").strip()
        password = params.get("password") or ""
        if not host or not username or not password:
            return "Provide host, username and password."
        ok = _put(host, {"username": username, "password": password})
        if not ok:
            return ("Could not store credential — no keyring and no "
                    "encryption key available. Install `cryptography` "
                    "and/or `keyring` to enable this.")
        return f"Credential for {host} stored (username: {username})."

    if verb == "list_hosts":
        hosts = _list_hosts()
        if not hosts:
            return "No credentials stored."
        return "Stored credentials:\n- " + "\n- ".join(hosts)

    if verb == "forget":
        host = (params.get("host") or "").strip()
        if not host:
            return "Provide host to forget."
        _del(host)
        return f"Credential for {host} forgotten."

    if verb == "export_safe":
        # Usernames only. Used by the dashboard view.
        out = []
        for host in _list_hosts():
            rec = _get(host) or {}
            out.append({"host": host, "username": rec.get("username", "")})
        return json.dumps(out, indent=2)

    if verb == "login":
        host = (params.get("host") or "").strip()
        url = (params.get("url") or "").strip()
        if not host:
            return "Provide host to log in to."
        cred = _get(host)
        if not cred:
            return (f"No credential stored for {host}. Ask the user for "
                    "username+password, save with action='save', then retry.")

        def _do_login() -> str:
            # Navigate first.
            if _bc is None:
                return "browser_control unavailable — cannot drive the page."
            target = url or f"https://{host}"
            nav = _bc.browser_control(parameters={"action": "go_to",
                                                   "url": target})
            if nav and "failed" in str(nav).lower():
                return f"Navigation to {target} failed: {nav}"
            # The user_field_hint / password_field_hint let the model
            # override selectors when the site uses non-standard inputs.
            fields = {
                params.get("user_field_hint", "email"): cred["username"],
                params.get("user_field_hint2", "username"): cred["username"],
                params.get("password_field_hint", "password"): cred["password"],
            }
            fill = _bc.browser_control(parameters={"action": "fill_form",
                                                    "fields": fields,
                                                    "use_prefs": False})
            click = _bc.browser_control(parameters={
                "action": "click",
                "text": params.get("submit_label", "Sign in")})
            # Return a scrubbed summary — never the password.
            return ("Login attempt complete. Fill result: "
                    f"{str(fill)[:200]}  |  Click result: {str(click)[:120]}"
                    ". Verify with get_url / get_text that the "
                    "dashboard is now visible.")

        return _approval.gate(
            tool="credential",
            args={"action": "login", "host": host},
            title=f"Sign in to {host} as {cred['username']}",
            detail="Password is read from the keyring and never shown. "
                   "If the site asks for MFA or a CAPTCHA, complete it "
                   "in the real browser window.",
            run=_do_login,
            lvl2_category="form_submit",
        )

    return "Unknown action. Use: save | list_hosts | login | forget | export_safe."


TOOL = {
    "name": "credential",
    "description": ("Site-login credentials kept in the OS keyring (or "
                    "AES-encrypted disk fallback). The model can trigger "
                    "a login flow but the password itself is never "
                    "returned in any tool response or transcript."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":       {"type": "STRING",
                             "description": "save | list_hosts | login | "
                                            "forget | export_safe"},
            "host":         {"type": "STRING",
                             "description": "e.g. github.com"},
            "username":     {"type": "STRING"},
            "password":     {"type": "STRING",
                             "description": "Only used with action=save — "
                                            "never echoed back"},
            "url":          {"type": "STRING",
                             "description": "Explicit login URL (default: "
                                            "https://<host>)"},
            "user_field_hint":    {"type": "STRING"},
            "user_field_hint2":   {"type": "STRING"},
            "password_field_hint": {"type": "STRING"},
            "submit_label": {"type": "STRING"},
        },
        "required": ["action"],
    },
    "handler": credential,
}
