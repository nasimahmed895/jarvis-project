"""
actions/form_fill.py — top-level profile-driven form filler.

Flow:
  1. Caller gives us a URL (optional — else the active tab) and the kind
     of form (registration / login / contact / application / profile /
     generic). We hand off to `browser_control` to navigate / read the
     page, then run `fill_form` with the values we pulled from the
     structured `memory/user_profile.py` store.
  2. If any REQUIRED field has no value in the profile, we surface the
     missing list back to the model instead of filling with blanks. The
     model then asks the user for the specific values.
  3. Submission is NOT performed here. Submission is gated through
     `core/approval.py` — a submit on an externally-visible form is LVL2
     (`form_submit` category) by default; the user may pre-approve.

This module is a composition, not a reimplementation: it never touches
Playwright directly. It uses `browser_control` the way the model would
— through its action verbs. That keeps all the existing gating,
AppleScript / CDP fall-backs, and session reuse in place.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from core import approval as _approval

try:
    from memory import user_profile as _profile
except Exception:
    _profile = None  # type: ignore

try:
    from actions import browser_control as _bc
except Exception:
    _bc = None  # type: ignore


# Default required fields per form kind. Real forms vary; this is the
# conservative baseline — anything else detected on the page becomes
# "opportunistic" and is filled only when the profile has a value.
_REQUIRED = {
    "registration": ("first_name", "last_name", "email"),
    "application":  ("first_name", "last_name", "email", "phone",
                     "resume_path"),
    "contact":      ("first_name", "email"),
    "profile":      (),
    "login":        (),  # credentials come from the keyring, not the profile
    "generic":      (),
}


def _run_bc(action: str, **params: Any) -> str:
    """Call browser_control with the current session context. Returns the
    tool's string result verbatim so the caller can relay failures."""
    if _bc is None:
        return "browser_control unavailable — install playwright."
    out = _bc.browser_control(
        parameters={"action": action, **params},
        response=None, player=None, session_memory=None)
    return out or ""


def form_fill(parameters: dict | None = None,
              response=None, player=None, speak=None,
              session_memory=None) -> str:
    params = parameters or {}
    kind = (params.get("kind") or "generic").lower().strip()
    url = (params.get("url") or "").strip()
    submit_after = bool(params.get("submit_after", False))
    required_extra = params.get("required_extra") or []

    if _profile is None:
        return "User profile store is not available."

    if kind not in _REQUIRED:
        return (f"Unknown kind '{kind}'. Use: "
                f"{' | '.join(_REQUIRED)}.")

    # Navigate if a URL was given; otherwise work on whatever tab is active.
    if url:
        nav = _run_bc("go_to", url=url)
        if "Error" in nav or "failed" in nav.lower():
            return f"Navigation failed: {nav}"

    # Check the profile has the required pieces before we touch the form.
    required = list(_REQUIRED[kind]) + [str(k) for k in required_extra]
    missing = _profile.missing(required)
    if missing and not params.get("ignore_missing"):
        labels = [_profile._BY_KEY[k].label for k in missing
                  if k in _profile._BY_KEY]
        return ("Cannot fill the form yet — the profile is missing: "
                + ", ".join(labels) + ". Ask the user for these, save "
                "them via user_profile, then retry.")

    # Grab everything we DO have. The browser_control.fill_form tool
    # auto-merges its own saved prefs on top; our structured profile
    # takes priority because it carries the typed field semantics.
    data = _profile.all()
    # Translate the profile keys into the vendor-neutral field names
    # browser_control.fill_form consumes. The underlying fill_form
    # matches these against label text, so we hand it both the canonical
    # key and the human label — whichever the page uses will hit.
    values: dict[str, str] = {}
    for key, value in data.items():
        if not value:
            continue
        field = _profile._BY_KEY.get(key)
        if field is None:
            continue
        values[key] = value
        values[field.label.lower()] = value

    # Delegate the actual filling. Pass use_prefs=True so the user's
    # ad-hoc set_pref values also contribute.
    res = _run_bc("fill_form", fields=values, use_prefs=True)

    # Pre-submission validation: reject FAILED fills immediately.
    if res.startswith("FAILED:"):
        return (
            "FILL FAILED — form was NOT touched. Cannot proceed to submit.\n"
            + res
            + "\nRun inspect_form, correct the field names, and retry."
        )

    if not submit_after:
        status = "PARTIAL" if res.startswith("PARTIAL:") else "COMPLETE"
        return (
            f"Form filled ({status}). Review the page and approve submit when ready.\n"
            "Fill result: " + res
        )

    # Refuse to submit when fill was only partial.
    if res.startswith("PARTIAL:"):
        return (
            "PARTIAL FILL — refusing to submit until all required fields are verified.\n"
            + res
            + "\nCorrect the missed fields, then retry with submit_after=true."
        )

    # Gated submit. Use core/approval so the HUD banner issues the token.
    def _do_submit() -> str:
        click_res = _run_bc("click", text=(params.get("submit_label") or "Submit"))

        # Post-submit verification: read page for confirmation or error.
        import time as _time
        _time.sleep(1.5)  # let the page settle after submit

        url_after = _run_bc("get_url")
        text_after = _run_bc("get_text")

        # Success signals
        success_patterns = (
            "success", "thank you", "confirmed", "submitted", "received",
            "congratulations", "complete", "done", "sent", "registered",
            "সফল", "ধন্যবাদ", "নিশ্চিত",  # Bengali equivalents
        )
        # Error signals
        error_patterns = (
            "error", "invalid", "required", "please fill", "failed",
            "try again", "went wrong", "ত্রুটি", "পূরণ করুন",
        )

        body_lower = (text_after or "").lower()
        url_changed = (url_after or "").strip() not in ("", url or "")

        has_success = any(p in body_lower for p in success_patterns)
        has_error   = any(p in body_lower for p in error_patterns)

        if has_error and not has_success:
            return (
                "SUBMISSION REJECTED — page shows errors after submit.\n"
                f"Submit click: {click_res}\n"
                f"URL after: {url_after}\n"
                f"Page signals: {', '.join(p for p in error_patterns if p in body_lower)}"
            )

        if has_success or url_changed:
            return (
                "SUBMISSION VERIFIED — success signal observed.\n"
                f"Submit click: {click_res}\n"
                f"URL after: {url_after}\n"
                f"Page signals: {', '.join(p for p in success_patterns if p in body_lower)}"
                + (" | URL changed" if url_changed else "")
            )

        return (
            "SUBMISSION ATTEMPTED — submit was clicked but result is ambiguous.\n"
            f"Submit click: {click_res}\n"
            f"URL after: {url_after}\n"
            "No clear success or error signal detected. Verify manually."
        )

    return _approval.gate(
        tool="form_fill",
        args={"action": "submit", "kind": kind, "url": url},
        title=f"Submit {kind} form"
              + (f" on {url}" if url else ""),
        detail="The form is filled with your saved profile values. "
               "This action will click the submit button.",
        run=_do_submit,
        lvl2_category="form_submit",
    )


TOOL = {
    "name": "form_fill",
    "description": ("Fill a web form on the current page (or navigate to "
                    "`url` first) using the structured user profile. "
                    "Supported kinds: registration, application, contact, "
                    "profile, login, generic. Submission is gated — set "
                    "submit_after=true to attempt a submit; the HUD will "
                    "ask the user to confirm."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING",
                     "description": "registration | application | contact "
                                    "| profile | login | generic"},
            "url":  {"type": "STRING",
                     "description": "Page to navigate to first (optional)"},
            "submit_after": {"type": "BOOLEAN",
                             "description": "Attempt a gated submit after fill"},
            "submit_label": {"type": "STRING",
                             "description": "Visible text of the submit button"},
            "required_extra": {"type": "ARRAY",
                               "items": {"type": "STRING"},
                               "description": "Extra profile keys required"},
            "ignore_missing": {"type": "BOOLEAN",
                               "description": "Fill partial even if required "
                                              "fields are missing"},
        },
        "required": [],
    },
    "handler": form_fill,
}
