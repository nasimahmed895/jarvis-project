"""
Browser multi-step flow runner with CAPTCHA pause/resume.

Wraps ``actions/browser_control.py`` with an orchestration layer:

  * run a sequence of browser_control steps in order
  * after each step, verify page state matches an optional expectation
  * detect CAPTCHA / bot-verification pages via DOM + text heuristics
  * on detection, PAUSE the sequence and surface the user-visible step
    so a human can complete the challenge in the real browser
  * expose a 'resume' entry so the planner can continue from where the
    pause happened once the user signals completion

This module NEVER bypasses CAPTCHA. The design is: detect, stop, hand
off to the user, resume once they confirm. Bypassing the detection or
automatically clicking through challenges is explicitly refused.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

try:
    from actions import browser_control as _bc
except Exception:  # pragma: no cover — avoids import cycle
    _bc = None  # type: ignore[assignment]


# ── CAPTCHA signatures ──────────────────────────────────────────────────────
# Three signal categories combine into a confidence score. One signal alone
# produces "likely"; two or more produces "detected". This keeps us from
# flagging every page that happens to contain the word "verify".

_CAPTCHA_URL_RE = re.compile(
    r"(google\.com/recaptcha|hcaptcha\.com|cloudflare\.com/challenge|"
    r"funcaptcha\.com|arkoselabs\.com|/captcha[/?])",
    re.IGNORECASE,
)

_CAPTCHA_TEXT_RE = re.compile(
    r"\b(i'm not a robot|prove you're human|verify you are human|"
    r"please complete the security check|one more step|"
    r"please verify to continue|checking if the site connection is secure|"
    r"please stand by, while we are checking|"
    r"press and hold to confirm you(?:'re|are) human)\b",
    re.IGNORECASE,
)

_CAPTCHA_DOM_HINTS = (
    "g-recaptcha", "recaptcha-anchor", "h-captcha", "cf-challenge",
    "turnstile", "funcaptcha", "px-captcha",
)


def detect_captcha_markers(*,
                            url: str = "",
                            text: str = "",
                            html: str = "") -> dict:
    """Pure-Python detector. Returns
    ``{likely, detected, confidence, reasons}``. Separated from the
    browser driver so it is unit-testable without launching Playwright."""
    reasons: list[str] = []
    score = 0

    if url and _CAPTCHA_URL_RE.search(url):
        reasons.append(f"captcha url pattern: {url[:120]}")
        score += 2

    if text and _CAPTCHA_TEXT_RE.search(text):
        m = _CAPTCHA_TEXT_RE.search(text)
        reasons.append(f"captcha text: {m.group(0)}" if m else "captcha text")
        score += 2

    hits_dom = [hint for hint in _CAPTCHA_DOM_HINTS if hint in (html or "").lower()]
    if hits_dom:
        reasons.append("dom hints: " + ", ".join(hits_dom[:3]))
        score += 1 + min(2, len(hits_dom) - 1)

    detected = score >= 2
    likely = score >= 1
    return {
        "likely":     likely,
        "detected":   detected,
        "confidence": min(1.0, score / 4.0),
        "reasons":    reasons,
    }


# ── Nav verifier ────────────────────────────────────────────────────────────

def verify_navigation(*, current_url: str, current_text: str,
                       expect_url_contains: str | None = None,
                       expect_page_contains: str | None = None,
                       expect_not_url: str | None = None) -> dict:
    """Return ``{ok, reasons}``. Any expectation that's set must match;
    expect_not_url is matched in the inverse direction (useful to detect
    a login-redirect that 'succeeded' by sending you back to /login)."""
    reasons: list[str] = []
    ok = True
    if expect_url_contains:
        if expect_url_contains.lower() not in (current_url or "").lower():
            ok = False
            reasons.append(f"url missing {expect_url_contains!r}")
        else:
            reasons.append(f"url ok ({expect_url_contains})")
    if expect_page_contains:
        if expect_page_contains.lower() not in (current_text or "").lower():
            ok = False
            reasons.append(f"page body missing {expect_page_contains!r}")
        else:
            reasons.append(f"page body ok ({expect_page_contains})")
    if expect_not_url:
        if expect_not_url.lower() in (current_url or "").lower():
            ok = False
            reasons.append(f"url contains forbidden {expect_not_url!r}")
    if not (expect_url_contains or expect_page_contains or expect_not_url):
        reasons.append("no expectation set")
    return {"ok": ok, "reasons": reasons}


# ── Flow runner ─────────────────────────────────────────────────────────────

def _run_browser_step(step: dict,
                       *, player=None, session_memory=None) -> str:
    """Delegate one step to ``browser_control``. Isolated so tests can
    swap it out. Raises RuntimeError if browser_control isn't importable."""
    if _bc is None:
        raise RuntimeError("browser_control is not available in this build")
    return _bc.browser_control(
        parameters=step,
        player=player,
        session_memory=session_memory,
    )


def _probe_page(step_params: dict, player=None,
                session_memory=None) -> tuple[str, str]:
    """Read current URL + body text so we can run detector + verifier.
    Uses browser_control's own actions so we never duplicate session
    knowledge. Returns ("", "") when it can't — detectors degrade safely."""
    browser = step_params.get("browser")
    url = ""
    text = ""
    for action in ("get_url", "current_url"):
        try:
            out = _run_browser_step({"action": action, "browser": browser},
                                     player=player,
                                     session_memory=session_memory)
            if isinstance(out, str) and ("://" in out or "http" in out):
                url = out
                break
        except Exception:
            continue
    try:
        text_out = _run_browser_step(
            {"action": "get_text", "browser": browser, "limit": 8000},
            player=player, session_memory=session_memory,
        )
        text = text_out if isinstance(text_out, str) else ""
    except Exception:
        pass
    return url, text


def _coerce_steps(raw) -> list[dict]:
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [s for s in data if isinstance(s, dict)]
        except Exception:
            pass
    return []


def run_sequence(steps: list[dict], *,
                  resume_from: int = 0,
                  verify: list[dict] | None = None,
                  player=None,
                  session_memory=None) -> dict:
    """Run ``steps[resume_from:]`` in order. After each step, read the
    page and check for CAPTCHA; if detected, return with ``status=
    'captcha_pause'`` and ``resume_step`` set to the next step index so
    the planner can call back here after the user has completed the
    challenge manually."""
    verify = verify or []
    results: list[dict] = []
    for i in range(resume_from, len(steps)):
        step = steps[i]
        try:
            out = _run_browser_step(step, player=player,
                                     session_memory=session_memory)
        except Exception as e:
            return {
                "status":      "error",
                "failed_step": i,
                "error":       f"{type(e).__name__}: {e}",
                "results":     results,
            }
        step_result = {"index": i, "step": step, "output": str(out)[:400]}

        # Probe the page once per step to feed detector + verifier.
        try:
            url, text = _probe_page(step, player=player,
                                      session_memory=session_memory)
        except Exception:
            url, text = "", ""

        det = detect_captcha_markers(url=url, text=text)
        if det["detected"]:
            step_result["captcha"] = det
            return {
                "status":        "captcha_pause",
                "resume_step":   i + 1,
                "message":       ("CAPTCHA detected — please complete the "
                                  "challenge in the browser, then call "
                                  "browser_flow action='resume' to "
                                  "continue."),
                "detector":      det,
                "current_url":   url,
                "results":       results + [step_result],
            }

        expectation = verify[i] if i < len(verify) else None
        if expectation:
            v = verify_navigation(current_url=url,
                                   current_text=text,
                                   expect_url_contains=expectation.get("url_contains"),
                                   expect_page_contains=expectation.get("page_contains"),
                                   expect_not_url=expectation.get("not_url"))
            step_result["verify"] = v
            if not v["ok"]:
                return {
                    "status":      "verify_failed",
                    "failed_step": i,
                    "verify":      v,
                    "results":     results + [step_result],
                }

        results.append(step_result)
        # Tiny breath so the browser can settle between steps (page
        # transitions sometimes trail JS loads).
        time.sleep(0.2)

    return {"status": "ok", "results": results}


# ── Entry point ─────────────────────────────────────────────────────────────

def browser_flow(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Multi-step browser workflow with CAPTCHA pause/resume.

    Actions:
      run_sequence     steps=<JSON list of browser_control parameter dicts>
                       verify=<JSON list of expectations per step> (optional)
      resume           steps=<JSON>  resume_from=<int>  verify=<JSON>
      detect_captcha   browser=  (reads current page)
      verify           browser=  url_contains= page_contains= not_url=
    """
    p = parameters or {}
    action = str(p.get("action", "")).lower().strip()

    if action in ("run_sequence", "resume"):
        steps = _coerce_steps(p.get("steps"))
        if not steps:
            return "browser_flow: provide 'steps' as a JSON list."
        try:
            resume_from = int(p.get("resume_from", 0))
        except (TypeError, ValueError):
            resume_from = 0
        verify = None
        v_raw = p.get("verify")
        if v_raw:
            verify = _coerce_steps(v_raw)
        result = run_sequence(steps, resume_from=resume_from,
                                verify=verify, player=player,
                                session_memory=session_memory)
        # Flatten into a readable string; JSON is also returned for
        # programmatic consumers via the 'raw' tail.
        lines = [f"status={result.get('status','?')}"]
        if result.get("status") == "captcha_pause":
            lines.append(f"PAUSED at step {result.get('resume_step')}")
            lines.append(result.get("message", ""))
            for r in (result.get("detector", {}).get("reasons") or [])[:3]:
                lines.append(f"  · {r}")
        elif result.get("status") == "verify_failed":
            lines.append(f"failed at step {result.get('failed_step')}")
            for r in (result.get("verify", {}).get("reasons") or []):
                lines.append(f"  · {r}")
        elif result.get("status") == "error":
            lines.append(f"error at step {result.get('failed_step')}: "
                         f"{result.get('error','?')}")
        else:
            lines.append(f"ran {len(result.get('results', []))} step(s)")
        return "\n".join(lines)

    if action == "detect_captcha":
        if _bc is None:
            return "browser_flow: browser_control unavailable."
        browser = p.get("browser")
        try:
            url, text = _probe_page({"browser": browser},
                                     player=player,
                                     session_memory=session_memory)
        except Exception as e:
            return f"browser_flow detect_captcha: {e}"
        det = detect_captcha_markers(url=url, text=text)
        return (f"captcha detected={det['detected']} "
                f"likely={det['likely']} confidence={det['confidence']:.2f}\n"
                + "\n".join("  · " + r for r in det["reasons"]))

    if action == "verify":
        if _bc is None:
            return "browser_flow: browser_control unavailable."
        browser = p.get("browser")
        try:
            url, text = _probe_page({"browser": browser},
                                     player=player,
                                     session_memory=session_memory)
        except Exception as e:
            return f"browser_flow verify: {e}"
        v = verify_navigation(
            current_url=url, current_text=text,
            expect_url_contains=p.get("url_contains"),
            expect_page_contains=p.get("page_contains"),
            expect_not_url=p.get("not_url"),
        )
        return (f"verify ok={v['ok']}\n"
                + "\n".join("  · " + r for r in v["reasons"]))

    return (f"browser_flow: unknown action {action!r}. "
            "Actions: run_sequence | resume | detect_captcha | verify")


TOOL = {
    "name": "browser_flow",
    "description": (
        "Multi-step browser workflow runner with CAPTCHA pause/resume. "
        "Pass a JSON array of browser_control parameter dicts (same "
        "shape as the browser_control tool) and an optional 'verify' "
        "list specifying what to expect after each step "
        "(url_contains, page_contains, not_url). After each step the "
        "page is scanned for reCAPTCHA / hCaptcha / Cloudflare / "
        "Turnstile markers; if one is detected the run PAUSES and "
        "returns a resume_step the user/planner can call back into once "
        "a human has completed the challenge in the real browser. "
        "CAPTCHAs are NEVER bypassed by the agent — detection → stop → "
        "human → resume. 'detect_captcha' and 'verify' let you probe the "
        "current page without running a sequence."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":       {"type": "STRING",
                             "description": "run_sequence | resume | detect_captcha | verify"},
            "steps":        {"type": "STRING",
                             "description": "JSON array of browser_control parameter dicts."},
            "verify":       {"type": "STRING",
                             "description": "JSON array of per-step expectations: {url_contains, page_contains, not_url}."},
            "resume_from":  {"type": "INTEGER",
                             "description": "Step index to resume at (set by a prior pause)."},
            "browser":      {"type": "STRING",
                             "description": "Browser to target for detect_captcha / verify."},
            "url_contains": {"type": "STRING", "description": "For 'verify'."},
            "page_contains":{"type": "STRING", "description": "For 'verify'."},
            "not_url":      {"type": "STRING", "description": "For 'verify': must NOT appear in URL."},
        },
        "required": ["action"],
    },
    "handler": browser_flow,
}
