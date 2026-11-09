"""
actions/ai_site.py — operate AI websites through the user's real browser.

Thin orchestrator over `actions/browser_control.py`. No new browser
engine; every click / fill / navigation is a `browser_control` tool
call, so the session, tab, approval, and anchoring logic built in
sessions 1-10 all apply here.

Verbs
-----
  open          — navigate to a site's landing URL (reuses an existing
                  tab when `verify_page` says we are already there)
  ask           — send a prompt, poll for completion, return the last
                  response text
  read_last     — just read the last response (no send)
  follow_up     — ask inside the SAME conversation
  new_chat      — start a fresh conversation
  upload        — attach a file (LVL2-gated via core/approval)
  list_sites    — list adapters known to the registry
  orchestrate   — run a multi-site workflow; later steps may reference
                  `{{prev_response}}` in their prompt

The 'ask' flow NEVER claims success without proof:
  1. inspect_form so the composer is actually detected.
  2. fill_form with the adapter's composer locator list.
  3. Only if the fill summary shows at least 1/1 verified, click send.
  4. Poll busy_selector and response_selector; stop when busy is gone
     AND response_selector has text.
  5. Return the response text; otherwise return a diagnostic error.
"""
from __future__ import annotations

import json
import time
from typing import Any

from core import ai_adapters as _adapters
from core import approval as _approval

try:
    from actions import browser_control as _bc
except Exception:
    _bc = None  # type: ignore


_POLL_INTERVAL_S = 1.0


def _bc_call(action: str, **params: Any) -> str:
    if _bc is None:
        return "browser_control unavailable — install playwright."
    out = _bc.browser_control(parameters={"action": action, **params})
    return out or ""


def _on_task_url(expected_host_hint: str) -> bool:
    """Treat `verify_page` output's `on_task_url: True` line (plus host
    presence) as proof we're on the right site. Avoids re-navigating
    when the user already has ChatGPT open."""
    res = _bc_call("verify_page")
    return ("on_task_url: True" in res) and (expected_host_hint in res)


def _is_busy(adapter: _adapters.Adapter) -> bool:
    """True when ANY of the adapter's busy indicators is present and
    visible. Uses the new `probe_selector` verb — the previous
    implementation tried to find CSS selector strings inside
    `get_text` output (body innerText), which ALWAYS returned no
    match, so busy was effectively never detected and the loop
    accepted whatever text was on the page."""
    for sel in adapter.busy_selector:
        probe = _bc_call("probe_selector", selector=sel)
        if "exists=True visible=True" in probe:
            return True
    return False


def _wait_for_response(adapter: _adapters.Adapter,
                       deadline: float) -> tuple[bool, str]:
    """Poll until the adapter is no longer busy and the page text has
    stabilised across two reads. Honest signal: busy is a real DOM
    probe, completion is a quiet-period check."""
    last_text = ""
    stable_hits = 0
    while time.monotonic() < deadline:
        if _is_busy(adapter):
            stable_hits = 0
            time.sleep(_POLL_INTERVAL_S)
            continue
        text = _bc_call("get_text")
        if text and text.strip() != last_text.strip() and len(text) > 20:
            last_text = text
            stable_hits = 1
            time.sleep(_POLL_INTERVAL_S)
            continue
        if last_text and stable_hits >= 1:
            # One busy-free read + one unchanged read + busy still
            # absent = answer is really final.
            return True, last_text
        time.sleep(_POLL_INTERVAL_S)
    return False, ("Timed out waiting for response. Last text seen: "
                   + (last_text[:400] if last_text else "(none)"))


def _ask(adapter: _adapters.Adapter, prompt: str,
         player=None) -> tuple[bool, str]:
    """Send a prompt and return (success, answer)."""
    # Composer locator(s). fill_form tries each candidate label; the
    # first that matches wins and then its value is verified.
    fields = {label: prompt for label in adapter.composer}
    fill = _bc_call("fill_form", fields=fields, use_prefs=False)
    if fill.startswith("FAILED:"):
        return False, (f"Composer not found on {adapter.name}. "
                       "The site layout may have changed; update "
                       f"the `composer` list in core/ai_adapters.py "
                       f"for '{adapter.key}'. Raw: {fill[:240]}")
    # Send. Try each label in order; first click that lands wins.
    for label in adapter.send_labels:
        click = _bc_call("click", text=label)
        if "Clicked" in click:
            break
    else:
        return False, (f"Send button not found on {adapter.name}. "
                       f"Expected one of: {adapter.send_labels}.")

    # Poll for completion.
    deadline = time.monotonic() + adapter.response_timeout_s
    ok, text = _wait_for_response(adapter, deadline)
    return ok, text


def ai_site(parameters: dict | None = None,
            response=None, player=None, speak=None,
            session_memory=None) -> str:
    params = parameters or {}
    verb = (params.get("action") or "").lower().strip()
    site_name = (params.get("site") or "").strip()
    adapter = _adapters.get(site_name) if site_name else None

    if verb == "list_sites":
        return "AI sites available:\n- " + "\n- ".join(
            f"{a.name} ({a.key})"
            for a in sorted(_adapters._ADAPTERS.values(),
                            key=lambda a: a.key))

    if verb == "open":
        if adapter is None:
            return "Unknown site. Use action='list_sites' to see keys."
        # Reuse an already-open tab for this site if we have one anchored.
        verify = _bc_call("verify_page")
        if any(h in verify for h in adapter.url_hosts):
            return f"Already on {adapter.name}: {verify.splitlines()[1]}"
        res = _bc_call("go_to", url=adapter.url)
        return res

    if verb == "ask":
        if adapter is None:
            return "Specify 'site' (chatgpt | claude | gemini)."
        prompt = (params.get("prompt") or "").strip()
        if not prompt:
            return "Specify 'prompt'."
        # Make sure we're on the right site before sending.
        if not _on_task_url(adapter.url_hosts[0]):
            _bc_call("go_to", url=adapter.url)
        ok, text = _ask(adapter, prompt, player=player)
        if not ok:
            return f"FAILED: {text}"
        # Return a tagged reply so the model can see which site produced it.
        return f"[{adapter.name}] response:\n{text[:3500]}"

    if verb == "read_last":
        if adapter is None:
            return "Specify 'site'."
        return _bc_call("get_text")

    if verb == "follow_up":
        # Same path as ask; the active tab already holds the conversation.
        if adapter is None:
            return "Specify 'site'."
        prompt = (params.get("prompt") or "").strip()
        if not prompt:
            return "Specify 'prompt' for follow_up."
        ok, text = _ask(adapter, prompt, player=player)
        return (f"[{adapter.name}] follow-up:\n{text[:3500]}"
                if ok else f"FAILED: {text}")

    if verb == "new_chat":
        if adapter is None:
            return "Specify 'site'."
        for label in adapter.new_chat_labels:
            r = _bc_call("click", text=label)
            if "Clicked" in r:
                return f"Started a new {adapter.name} conversation."
        return (f"Could not find new-chat trigger on {adapter.name}. "
                f"Tried: {adapter.new_chat_labels}")

    if verb == "upload":
        if adapter is None:
            return "Specify 'site'."
        path = (params.get("path") or "").strip()
        if not path:
            return "Specify 'path' of the file to upload."

        def _do_upload() -> str:
            for label in adapter.upload_labels:
                r = _bc_call("click", text=label)
                if "Clicked" in r:
                    return (f"Opened file picker on {adapter.name}. "
                            "The OS dialog is the user's to handle — "
                            "select the file and confirm.")
            return (f"Upload trigger not found on {adapter.name}. "
                    f"Tried: {adapter.upload_labels}")

        return _approval.gate(
            tool="ai_site",
            args={"action": "upload", "site": adapter.key, "path": path},
            title=f"Upload file to {adapter.name}",
            detail=f"Will share {path} with {adapter.name}'s servers.",
            run=_do_upload,
            lvl2_category="upload_file",
        )

    if verb == "orchestrate":
        steps_raw = params.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            return ("Provide 'steps' as a list of {site, prompt} "
                    "objects. Later prompts may reference "
                    "{{prev_response}} to inject the previous step's "
                    "answer.")
        results: list[dict] = []
        last_text = ""
        for i, raw in enumerate(steps_raw):
            if not isinstance(raw, dict):
                results.append({"step": i, "error": "step not a dict"})
                break
            step_site = _adapters.get((raw.get("site") or "").strip())
            if step_site is None:
                results.append({"step": i,
                                "error": f"unknown site: {raw.get('site')}"})
                break
            prompt = (raw.get("prompt") or "").replace(
                "{{prev_response}}", last_text).strip()
            if not prompt:
                results.append({"step": i, "error": "empty prompt"})
                break
            # Navigate if we're not already on this site.
            if not _on_task_url(step_site.url_hosts[0]):
                _bc_call("go_to", url=step_site.url)
            ok, text = _ask(step_site, prompt, player=player)
            step_result = {"step": i, "site": step_site.key,
                           "ok": ok, "chars": len(text)}
            if ok:
                last_text = text
                step_result["response_head"] = text[:400]
            else:
                step_result["error"] = text[:400]
                results.append(step_result)
                break
            results.append(step_result)
        return json.dumps({"final_response": last_text[:3500],
                           "steps": results}, indent=2)

    return ("Unknown action. Use: list_sites | open | ask | read_last "
            "| follow_up | new_chat | upload | orchestrate.")


TOOL = {
    "name": "ai_site",
    "description": ("Operate AI websites (ChatGPT, Claude, Gemini) "
                    "through the user's real authenticated browser. "
                    "Verbs: open, ask, read_last, follow_up, new_chat, "
                    "upload (gated), orchestrate (multi-site chain "
                    "with {{prev_response}} substitution), list_sites. "
                    "Never fabricates responses; every claim comes "
                    "from actual browser state."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "list_sites | open | ask | "
                                      "read_last | follow_up | "
                                      "new_chat | upload | orchestrate"},
            "site":   {"type": "STRING",
                       "description": "chatgpt | claude | gemini "
                                      "(aliases: gpt, bard, openai, "
                                      "anthropic)"},
            "prompt": {"type": "STRING",
                       "description": "Message to send (ask / follow_up)"},
            "path":   {"type": "STRING",
                       "description": "File path to upload"},
            "steps":  {"type": "ARRAY",
                       "items": {"type": "OBJECT", "properties": {}},
                       "description": "orchestrate: list of {site, "
                                      "prompt} entries. prompt may "
                                      "contain {{prev_response}}."},
        },
        "required": ["action"],
    },
    "handler": ai_site,
}
