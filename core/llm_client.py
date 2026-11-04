"""
LLM client for MARK XL.

Backends — selected via  "llm_provider"  in config/api_keys.json:

  "llm_provider": "ollama"   (default)
        Ollama's native /api/chat endpoint. https://ollama.com  (port 11434)

  "llm_provider": "openai" | "openrouter" | "groq" | "lmstudio" | "localai" | "jan" | "llamacpp"
        Any OpenAI-compatible /v1/chat/completions server.
        For cloud (OpenAI, OpenRouter, Groq) also set "llm_api_key".
        URLs:
          OpenAI      https://api.openai.com
          OpenRouter  https://openrouter.ai/api
          Groq        https://api.groq.com/openai
        Local servers (LM Studio http://localhost:1234, etc.) need no key.
        Tool-calling support depends on the model.

  "llm_provider": "anthropic"
        Anthropic /v1/messages. Set "llm_api_key" to sk-ant-...
        Default URL: https://api.anthropic.com
        Non-streaming only (stream falls back to non-stream call).
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Generator

import requests

# Matches a sentence boundary: [.!?] followed by whitespace, or a blank line.
# Avoids splitting on decimals (3.5) because those have no space after the dot.
_SENT_END = re.compile(r'(?<=[.!?])\s+|(?<=\n)\s*\n')

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR    = get_base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

_DEFAULTS = {
    "llm_url":      "http://localhost:11434",
    "llm_model":    "llama3.2",
    "llm_provider": "ollama",   # "ollama" | "openai" | "anthropic"
}

# Provider → default base URL (used when "llm_url" is missing from config).
_PROVIDER_URLS = {
    "openai":     "https://api.openai.com",
    "openrouter": "https://openrouter.ai/api",
    "groq":       "https://api.groq.com/openai",
    "anthropic":  "https://api.anthropic.com",
}

_ANTHROPIC_VERSION = "2023-06-01"


def get_llm_provider() -> str:
    """
    Normalises the raw provider string to one of:
        'ollama', 'openai' (covers OpenAI, OpenRouter, Groq, LM Studio, …),
        'anthropic'.
    """
    raw = _load_config().get("llm_provider", "ollama").strip().lower()
    if raw == "anthropic":
        return "anthropic"
    if raw in ("openai", "openrouter", "groq", "lmstudio", "localai", "jan", "llamacpp"):
        return "openai"
    return "ollama"


def get_llm_api_key() -> str | None:
    """API key for cloud providers (OpenAI, OpenRouter, Groq, Anthropic)."""
    key = _load_config().get("llm_api_key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def _auth_headers() -> dict:
    """Bearer header for OpenAI-compatible endpoints. Empty when no key set."""
    key = get_llm_api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _anthropic_headers() -> dict:
    return {
        "x-api-key":         get_llm_api_key() or "",
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type":      "application/json",
    }


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def ensure_ollama_running(timeout: int = 15) -> bool:
    """
    For Ollama: ping /api/tags; auto-launch 'ollama serve' if not running.
    For OpenAI-compatible providers: just ping /v1/models (server must be started manually).
    Returns True if the LLM server is reachable.
    """
    url, _   = get_llm_settings()
    provider = get_llm_provider()

    if provider == "anthropic":
        # Anthropic has no public liveness probe; we just verify DNS + TLS reach
        # by hitting /v1/models (returns 200 with a valid key, 401 without).
        # Either is "reachable" from this function's perspective.
        try:
            resp = requests.get(f"{url}/v1/models", headers=_anthropic_headers(), timeout=5)
            if resp.status_code in (200, 401):
                print(f"[LLM] Anthropic endpoint reachable at {url} (status {resp.status_code}).")
                return True
            print(f"[LLM] Anthropic endpoint returned status {resp.status_code}.")
            return False
        except Exception as e:
            print(f"[LLM] Cannot reach Anthropic endpoint at {url}: {e}")
            return False

    if provider == "openai":
        # OpenAI-compatible servers: cloud (OpenAI/OpenRouter/Groq) or local
        # (LM Studio/LocalAI/Jan). Local servers must be started by the user.
        health = f"{url}/v1/models"
        try:
            ok = requests.get(health, headers=_auth_headers(), timeout=5).status_code == 200
            if ok:
                print(f"[LLM] OpenAI-compatible server reachable at {url}")
            else:
                print(f"[LLM] Server at {url} returned non-200.  Is it running / is the key correct?")
            return ok
        except Exception as e:
            print(
                f"[LLM] Cannot reach OpenAI-compatible server at {url}.\n"
                "      Local: start LM Studio / LocalAI / Jan.\n"
                "      Cloud: check llm_api_key + network."
            )
            return False

    # ── Ollama ──────────────────────────────────────────────────────────────
    health = f"{url}/api/tags"

    def _is_up() -> bool:
        try:
            return requests.get(health, timeout=3).status_code == 200
        except Exception:
            return False

    if _is_up():
        return True

    print("[LLM] Ollama not running — launching 'ollama serve'…")
    try:
        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(["ollama", "serve"], **kwargs)
    except FileNotFoundError:
        print("[LLM] 'ollama' command not found. Install Ollama from https://ollama.com")
        return False
    except Exception as e:
        print(f"[LLM] Could not launch Ollama: {e}")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        if _is_up():
            print("[LLM] Ollama started successfully.")
            return True

    print("[LLM] Ollama did not respond within the timeout.")
    return False


def warmup_model(system_prompt: str | None = None) -> bool:
    """
    Pre-load the model AND prime Ollama's KV prefix cache.

    Why the system_prompt matters
    ─────────────────────────────
    Ollama caches the KV attention state of the prompt prefix across requests.
    If warmup includes the same system prompt that real requests will use, Ollama
    evaluates those tokens ONCE at startup.  Every subsequent request only needs
    to evaluate the small delta (user message ± time context) instead of the full
    300-500 token system prompt → drops first-token latency from ~17 s to <1 s.

    Pass the *static* part of the system prompt (the JARVIS protocol text, without
    timestamps or per-minute context) so the prefix stays valid across calls.
    """
    url, model = get_llm_settings()
    provider   = get_llm_provider()
    print(f"[LLM] Warming up '{model}' ({provider})…")

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": "hi"})

    if provider == "anthropic":
        # No warmup contract for Anthropic — cloud models are always "warm".
        # Skip the ping so we don't spend tokens for nothing.
        print(f"[LLM] '{model}' ready (Anthropic, no warmup).")
        return True

    if provider == "openai":
        # OpenAI-compatible: just fire a minimal request to ensure the model is loaded.
        # No keep_alive or KV-cache priming available — server manages this internally.
        payload = {
            "model":      model,
            "messages":   messages,
            "stream":     False,
            "max_tokens": 1,
        }
        try:
            resp = requests.post(
                f"{url}/v1/chat/completions",
                json=payload,
                headers=_auth_headers(),
                timeout=180,
            )
            resp.raise_for_status()
            print(f"[LLM] '{model}' ready (OpenAI-compatible server).")
            return True
        except Exception as e:
            print(f"[LLM] Warmup failed (non-fatal): {e}")
            return False

    # ── Ollama ──────────────────────────────────────────────────────────────
    payload = {
        "model":      model,
        "messages":   messages,
        "stream":     False,
        "keep_alive": -1,
        # num_gpu:99 → push ALL transformer layers to GPU (Ollama caps at available)
        # This is safe even without a GPU — Ollama silently ignores if n_gpu_layers=0
        "options":    {"num_predict": 1, "num_gpu": 99},
    }
    try:
        resp = requests.post(f"{url}/api/chat", json=payload, timeout=180)
        resp.raise_for_status()
        print(f"[LLM] '{model}' loaded and KV cache primed.")
        return True
    except Exception as e:
        print(f"[LLM] Warmup failed (non-fatal): {e}")
        return False


def check_model_available(log: Callable | None = None) -> bool:
    """
    Returns True if the configured model is already pulled in Ollama.
    Logs an actionable warning (to console + optional UI callback) if not.
    Always returns True for non-Ollama providers (cannot inspect their model list).
    """
    if get_llm_provider() != "ollama":
        return True

    url, model = get_llm_settings()
    try:
        resp = requests.get(f"{url}/api/tags", timeout=5)
        resp.raise_for_status()
        pulled = [m.get("name", "") for m in resp.json().get("models", [])]
        model_base = model.split(":")[0]
        found = any(
            m == model or m == model_base or m.startswith(model_base + ":")
            for m in pulled
        )
        if not found:
            available = ", ".join(pulled) if pulled else "none"
            warn = (
                f"WRN: Model '{model}' is not pulled in Ollama.\n"
                f"     Available: {available}\n"
                f"     Fix: ollama pull {model}"
            )
            print(warn)
            if log:
                log(f"WRN: '{model}' not found — run: ollama pull {model}")
        return found
    except Exception:
        return True   # Ollama might still be starting up; non-blocking


def get_llm_settings() -> tuple[str, str]:
    """
    Returns (base_url, model_name).

    If 'llm_url' is not set, pick a provider-appropriate default so cloud users
    only need llm_provider + llm_api_key + llm_model.
    """
    cfg      = _load_config()
    raw_prov = cfg.get("llm_provider", _DEFAULTS["llm_provider"]).strip().lower()
    default  = _PROVIDER_URLS.get(raw_prov, _DEFAULTS["llm_url"])
    url      = cfg.get("llm_url", default).rstrip("/")
    model    = cfg.get("llm_model", _DEFAULTS["llm_model"])
    return url, model


def _split_sentences(text: str) -> list[str]:
    """Split a block of text on sentence boundaries, keep order, drop empties."""
    out: list[str] = []
    buf = text
    while True:
        m = _SENT_END.search(buf)
        if not m:
            break
        sent = buf[: m.start() + 1].strip()
        buf  = buf[m.end():]
        if sent:
            out.append(sent)
    if buf.strip():
        out.append(buf.strip())
    return out


def _anthropic_tools(tools: list) -> list:
    """Convert OpenAI-style tool specs to Anthropic input_schema format."""
    out = []
    for t in tools:
        fn = t.get("function") or t
        out.append({
            "name":        fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or fn.get("input_schema") or {},
        })
    return out


def _call_anthropic(
    url:      str,
    model:    str,
    messages: list,
    tools:    list | None,
    timeout:  int,
) -> dict:
    """
    Non-streaming Anthropic /v1/messages call. Returns the Ollama-shaped dict
    {"content": str, "tool_calls": list} so callers need no provider branch.
    """
    # Anthropic puts system prompts in a top-level field, not in messages.
    sys_parts: list[str] = []
    convo:     list[dict] = []
    for m in messages:
        role    = m.get("role")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                sys_parts.append(content)
            continue
        if role == "tool":
            # Map OpenAI tool-result messages → Anthropic tool_result blocks.
            convo.append({
                "role": "user",
                "content": [{
                    "type":        "tool_result",
                    "tool_use_id": m.get("tool_call_id") or m.get("id") or "",
                    "content":     content if isinstance(content, str) else json.dumps(content),
                }],
            })
            continue
        if role == "assistant" and m.get("tool_calls"):
            # Replay OpenAI-style assistant tool_calls as Anthropic tool_use blocks.
            blocks: list[dict] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            for tc in m["tool_calls"]:
                fn    = tc.get("function", {})
                args  = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"_raw": args}
                blocks.append({
                    "type":  "tool_use",
                    "id":    tc.get("id", ""),
                    "name":  fn.get("name", ""),
                    "input": args or {},
                })
            convo.append({"role": "assistant", "content": blocks})
            continue
        convo.append({"role": role, "content": content})

    payload: dict = {
        "model":      model,
        "messages":   convo,
        "max_tokens": 1024,
    }
    if sys_parts:
        payload["system"] = "\n\n".join(sys_parts)
    if tools:
        payload["tools"] = _anthropic_tools(tools)

    try:
        resp = requests.post(
            f"{url}/v1/messages",
            json=payload,
            headers=_anthropic_headers(),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        raise RuntimeError("Anthropic request timed out.")
    except requests.exceptions.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        raise RuntimeError(f"Anthropic HTTP {e.response.status_code}: {body}")
    except Exception as e:
        raise RuntimeError(f"Anthropic call failed: {e}")

    content_text = ""
    tc_list:  list = []
    for block in data.get("content", []):
        btype = block.get("type")
        if btype == "text":
            content_text += block.get("text", "")
        elif btype == "tool_use":
            tc_list.append({
                "id":       block.get("id", ""),
                "function": {
                    "name":      block.get("name", ""),
                    "arguments": block.get("input") or {},
                },
            })
    return {"content": content_text.strip(), "tool_calls": tc_list}


def call_llm(
    messages: list,
    tools:    list | None = None,
    timeout:  int = 120,
) -> dict:
    """
    Non-streaming chat request.  Routes to Ollama or OpenAI-compatible backend.

    Returns:
        {"content": str, "tool_calls": list}
    """
    url, model = get_llm_settings()
    provider   = get_llm_provider()

    if provider == "anthropic":
        return _call_anthropic(url, model, messages, tools, timeout)

    if provider == "openai":
        endpoint = f"{url}/v1/chat/completions"
        payload: dict = {
            "model":      model,
            "messages":   messages,
            "stream":     False,
            "max_tokens": 150,
        }
        if tools:
            payload["tools"]       = tools
            payload["tool_choice"] = "auto"
        try:
            resp = requests.post(endpoint, json=payload, headers=_auth_headers(), timeout=timeout)
            resp.raise_for_status()
            choice = resp.json().get("choices", [{}])[0]
            msg    = choice.get("message", {})
            # OpenAI tool_calls format → normalise to Ollama-style
            raw_tc  = msg.get("tool_calls") or []
            tc_list = [
                {
                    "id":       t.get("id", ""),
                    "function": {
                        "name":      t["function"]["name"],
                        "arguments": (
                            json.loads(t["function"]["arguments"])
                            if isinstance(t["function"].get("arguments"), str)
                            else t["function"].get("arguments", {})
                        ),
                    },
                }
                for t in raw_tc
            ]
            return {
                "content":    (msg.get("content") or "").strip(),
                "tool_calls": tc_list,
            }
        except Exception as e:
            raise RuntimeError(f"OpenAI-compatible LLM call failed: {e}")

    # ── Ollama ──────────────────────────────────────────────────────────────
    endpoint = f"{url}/api/chat"
    payload = {
        "model":      model,
        "messages":   messages,
        "stream":     False,
        "keep_alive": -1,
        "options":    {"num_predict": 150, "num_gpu": 99},
    }
    if tools:
        payload["tools"] = tools

    try:
        resp = requests.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        msg  = data.get("message", {})
        return {
            "content":    (msg.get("content") or "").strip(),
            "tool_calls": msg.get("tool_calls") or [],
        }
    except requests.exceptions.ConnectionError as e:
        print(f"[LLM] ConnectionError — trying to restart Ollama… ({e})")
        if ensure_ollama_running():
            try:
                resp = requests.post(endpoint, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                msg  = data.get("message", {})
                return {
                    "content":    (msg.get("content") or "").strip(),
                    "tool_calls": msg.get("tool_calls") or [],
                }
            except Exception:
                pass
        raise RuntimeError(
            f"Cannot connect to Ollama at {url}. "
            "Make sure Ollama is installed and run: ollama serve"
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("Ollama request timed out after 120 s.")
    except requests.exceptions.HTTPError as e:
        print(f"[LLM] HTTPError: {e.response.status_code} — {e.response.text[:200]}")
        raise RuntimeError(f"Ollama HTTP error: {e.response.status_code}")
    except Exception as e:
        print(f"[LLM] Unexpected error: {type(e).__name__}: {e}")
        raise RuntimeError(f"LLM call failed: {e}")


def call_llm_text(
    prompt:  str,
    system:  str | None = None,
    model:   str | None = None,
    timeout: int = 120,
) -> str:
    """
    Simple text-only generation (no tools).
    Used by planner, executor, error_handler, code_helper, dev_agent.

    Routes by provider so cloud backends work from the same helper.
    """
    url, default_model = get_llm_settings()
    provider = get_llm_provider()
    m        = model or default_model

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    if provider == "anthropic":
        result = _call_anthropic(url, m, messages, None, timeout)
        return (result.get("content") or "").strip()

    if provider == "openai":
        endpoint = f"{url}/v1/chat/completions"
        payload  = {"model": m, "messages": messages, "stream": False, "max_tokens": 600}
        try:
            resp = requests.post(endpoint, json=payload, headers=_auth_headers(), timeout=timeout)
            resp.raise_for_status()
            choice = resp.json().get("choices", [{}])[0]
            return (choice.get("message", {}).get("content") or "").strip()
        except Exception as e:
            raise RuntimeError(f"OpenAI-compatible text call failed: {e}")

    # ── Ollama ──────────────────────────────────────────────────────────────
    endpoint = f"{url}/api/chat"
    payload  = {"model": m, "messages": messages, "stream": False, "keep_alive": -1, "options": {"num_predict": 600}}

    try:
        resp = requests.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
        return (resp.json().get("message", {}).get("content") or "").strip()
    except requests.exceptions.ConnectionError:
        if ensure_ollama_running():
            try:
                resp = requests.post(endpoint, json=payload, timeout=timeout)
                resp.raise_for_status()
                return (resp.json().get("message", {}).get("content") or "").strip()
            except Exception:
                pass
        raise RuntimeError(
            f"Cannot connect to Ollama at {url}. "
            "Make sure Ollama is installed and run: ollama serve"
        )
    except Exception as e:
        raise RuntimeError(f"LLM text call failed: {e}")


def _stream_openai(
    messages: list,
    tools:    list | None,
    timeout:  int,
) -> Generator[dict, None, None]:
    """
    Streaming backend for OpenAI-compatible servers (LM Studio, LocalAI, Jan…).

    Parses Server-Sent Events (SSE) and accumulates streaming tool-call fragments
    so the output format is identical to the Ollama backend.
    """
    url, model = get_llm_settings()
    endpoint   = f"{url}/v1/chat/completions"

    payload: dict = {
        "model":      model,
        "messages":   messages,
        "stream":     True,
        "max_tokens": 150,
    }
    if tools:
        payload["tools"]       = tools
        payload["tool_choice"] = "auto"

    try:
        with requests.post(
            endpoint,
            json=payload,
            headers=_auth_headers(),
            timeout=timeout,
            stream=True,
        ) as resp:
            resp.raise_for_status()
            full_content = ""
            buf          = ""
            # tool_call fragments: index → {"id", "function": {"name", "arguments"}}
            tc_fragments: dict[int, dict] = {}

            for raw in resp.iter_lines():
                if not raw:
                    continue
                # SSE lines look like: b"data: {...}" or b"data: [DONE]"
                line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choice = chunk.get("choices", [{}])[0]
                delta  = choice.get("delta", {})
                text   = delta.get("content") or ""

                full_content += text
                buf          += text

                # Accumulate sentence boundaries for streaming TTS
                while True:
                    m = _SENT_END.search(buf)
                    if not m:
                        break
                    sentence = buf[: m.start() + 1].strip()
                    buf      = buf[m.end():]
                    if sentence:
                        yield {"type": "sentence", "text": sentence}

                # Accumulate streaming tool-call fragments
                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    if idx not in tc_fragments:
                        tc_fragments[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                    frag = tc_fragments[idx]
                    frag["id"] = frag["id"] or tc.get("id", "")
                    fn = tc.get("function", {})
                    frag["function"]["name"]      += fn.get("name") or ""
                    frag["function"]["arguments"] += fn.get("arguments") or ""

                finish = choice.get("finish_reason")
                if finish in ("stop", "tool_calls", "length"):
                    break

            # Flush any trailing content
            if buf.strip():
                yield {"type": "sentence", "text": buf.strip()}

            # Parse accumulated tool-call argument strings → dicts
            tool_calls: list = []
            for idx in sorted(tc_fragments):
                frag = tc_fragments[idx]
                args = frag["function"]["arguments"]
                try:
                    args = json.loads(args)
                except Exception:
                    pass   # leave as raw string; _execute_tool handles it
                tool_calls.append({
                    "id":       frag["id"],
                    "function": {"name": frag["function"]["name"], "arguments": args},
                })

            yield {
                "type":       "done",
                "content":    full_content.strip(),
                "tool_calls": tool_calls,
            }

    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"Cannot reach OpenAI-compatible server at {url}.\n"
            "Make sure LM Studio / LocalAI / Jan is running and the server is started."
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("OpenAI-compatible stream timed out.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"OpenAI-compatible HTTP error: {e.response.status_code}")
    except Exception as e:
        raise RuntimeError(f"OpenAI-compatible stream failed: {e}")


def call_llm_stream(
    messages: list,
    tools:    list | None = None,
    timeout:  int = 120,
) -> Generator[dict, None, None]:
    """
    Streaming chat request.  Routes to Ollama or OpenAI-compatible backend.

    Yields:
        {"type": "sentence", "text": str}   — each complete sentence as it arrives
        {"type": "done", "content": str, "tool_calls": list}  — when stream ends

    Sentences are split on [.!?] + whitespace so TTS can start immediately.
    Tool calls always appear in the final "done" event.
    """
    provider = get_llm_provider()
    if provider == "openai":
        yield from _stream_openai(messages, tools, timeout)
        return
    if provider == "anthropic":
        # Anthropic streaming parser isn't implemented — fall back to non-stream,
        # then split the reply into sentences so downstream TTS still works.
        result = call_llm(messages, tools, timeout)
        text   = result.get("content") or ""
        for sent in _split_sentences(text):
            if sent:
                yield {"type": "sentence", "text": sent}
        yield {
            "type":       "done",
            "content":    text.strip(),
            "tool_calls": result.get("tool_calls") or [],
        }
        return

    url, model = get_llm_settings()
    endpoint   = f"{url}/api/chat"

    payload: dict = {
        "model":      model,
        "messages":   messages,
        "stream":     True,
        "keep_alive": -1,
        # 150 tokens ≈ 100 words ≈ 3-4 sentences — enough for any voice reply.
        # num_gpu:99 pushes all layers to GPU; num_thread removed (Ollama auto-tunes).
        "options":    {"num_predict": 150, "num_gpu": 99},
    }
    if tools:
        payload["tools"] = tools

    def _do_stream() -> Generator[dict, None, None]:
        with requests.post(endpoint, json=payload, timeout=timeout, stream=True) as resp:
            resp.raise_for_status()
            full_content = ""
            tool_calls:  list = []
            buf          = ""

            for raw in resp.iter_lines():
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg   = chunk.get("message", {})
                delta = msg.get("content") or ""

                full_content += delta
                buf          += delta

                # Yield complete sentences as they accumulate
                while True:
                    m = _SENT_END.search(buf)
                    if not m:
                        break
                    sentence = buf[: m.start() + 1].strip()
                    buf      = buf[m.end() :]
                    if sentence:
                        yield {"type": "sentence", "text": sentence}

                tc = msg.get("tool_calls")
                if tc:
                    tool_calls.extend(tc)

                if chunk.get("done"):
                    if buf.strip():
                        yield {"type": "sentence", "text": buf.strip()}

                    yield {
                        "type":       "done",
                        "content":    full_content.strip(),
                        "tool_calls": tool_calls,
                    }
                    return

    try:
        yield from _do_stream()
    except requests.exceptions.ConnectionError as e:
        print(f"[LLM] Stream ConnectionError — trying to restart Ollama… ({e})")
        if ensure_ollama_running():
            yield from _do_stream()
            return
        raise RuntimeError(
            f"Cannot connect to Ollama at {url}. "
            "Make sure Ollama is installed and run: ollama serve"
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("Ollama stream timed out.")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Ollama HTTP error: {e.response.status_code}")
    except Exception as e:
        print(f"[LLM] Stream error: {type(e).__name__}: {e}")
        raise RuntimeError(f"LLM stream failed: {e}")
