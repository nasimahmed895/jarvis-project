# Jarvis Voice AI Assistant — Audit Report

_Read-only architecture, security, and reliability audit. No project files were modified. No secret values are reproduced here._

**Audit date:** 2026-10-12
**Scope:** `/Users/pcm1-v4/Downloads/Mark-LV-main` at branch `main`, commit `5ff7630`.

---

## 1. Executive Summary

Jarvis ("MARK LIV") is a desktop PyQt6 voice assistant wired to Google's **Gemini Live API** for duplex audio. Core = `main.py` (2365 LOC, single `JarvisLive` class driving one asyncio TaskGroup) + `ui.py` (6591 LOC Qt HUD) + 17 auto-discovered action tools + plugin system + optional phone dashboard over FastAPI.

**Strengths observed**
- Clean plugin/action auto-discovery contract (TOOL/PLUGIN dict + handler) — adding a tool is one file.
- Gemini ladder (`core/gemini.py`) with Live-first routing, measured per-model cooldowns, and quota recovery.
- Session resumption handles plumbed through reconnects; sliding-window compression on.
- Push-to-talk, wake word, echo guard, barge-in and viseme-driven avatar are all real, not stubs.
- Undo stack and confirm gate exist for destructive OS actions.

**Reliability gaps**
- No automated tests of any kind.
- One monolithic receive loop handles transcripts, tool calls, vision injection, dashboard broadcast; failure anywhere raises and reconnects the whole session.
- Multi-step/compound task execution is implicit (model-driven) with **no planner, no verifier, no retry policy**.
- Vision has a single 4s cooldown; no watchdog for stuck `_vision_busy=True` beyond the next capture.

**Security gaps (highest)**
- `config/api_keys.json` stores Gemini key in **plaintext** on disk (observed; value not reproduced here).
- Several actions execute **model-generated code or shell commands** (`code_helper`, `dev_agent`, `desktop_control` sandbox) with no confirm gate.
- `computer_control` and `browser_control` can drive mouse/keyboard without confirmation.
- No per-tool permission model; the model's decision is the only gatekeeper.

**Capability vs. the user's stated goal (modular, planning, verifying, recovering assistant)**
- Modular skill registry: **Implemented** (action/plugin loaders).
- Structured tool calling: **Implemented** (Gemini function declarations).
- Multi-step planning: **Missing** (no planner module; relies on model to chain calls per turn).
- Result verification: **Missing** (tool result returned verbatim as `{"result": …}`).
- Error recovery: **Partial** (per-call try/except → spoken error; no retry/replan).
- Persistent memory: **Partial** (keyword search over JSON, no embeddings).
- Permission-controlled OS access: **Partial** (`confirm.py` used only for shutdown/restart/WiFi).

---

## 2. Verified Technology Stack

| Layer | Verified from file |
|---|---|
| Language | Python ≥ 3.11, ≤ 3.13 (`setup.py:19-20`) |
| GUI | PyQt6 (`requirements.txt`, `ui.py`) |
| Live LLM | `google-genai >= 2.8.0, <3` (Live API) |
| Models | `models/gemini-3.1-flash-live-preview` + 2.5 native-audio fallback (`core/gemini.py:162-165`) |
| Audio I/O | `sounddevice` + `numpy` PCM16 @ 16 kHz in / 24 kHz out (`main.py:109-111`) |
| Wake word | `openwakeword` (opt-in, lazy install) |
| Vision | `mss`, `opencv-python`, `pillow` |
| Automation | `pyautogui`, `pyperclip`, `playwright` |
| Browsers | Chromium + Firefox via Playwright (`setup.py:84`) |
| Dashboard | `fastapi`, `uvicorn`, `cryptography` (self-signed cert) |
| TTS/STT engines (optional) | EdgeTTS / Kokoro / ElevenLabs; faster-whisper / vosk (`core/tts.py`, `core/stt.py`) — unused in the main voice loop |
| Platforms | Windows / macOS / Linux (`sys_platform` markers) |

**Not using**: LangChain / LangGraph / Semantic Kernel / Pydantic AI / any planning framework.

---

## 3. Project Directory and Module Map

```
Mark-LV-main/
├── main.py                     ← entry; JarvisLive class, Live session loop (2365 lines)
├── ui.py                       ← PyQt6 HUD + avatar + content panel (6591 lines)
├── setup.py                    ← installer only (not a Python package)
├── requirements.txt
├── readme.md
├── config/
│   ├── api_keys.json           ← Gemini key (PLAINTEXT) + flags
│   ├── certs/                  ← dashboard TLS (self-signed)
│   └── jarvis.ico
├── core/
│   ├── gemini.py               ← one-shot Gemini ladder (Live→REST), cooldowns
│   ├── action_loader.py        ← actions/*.py TOOL-dict discovery
│   ├── plugin_loader.py        ← plugins/*.py PLUGIN-dict discovery
│   ├── confirm.py              ← HUD confirm gate (90s timeout)
│   ├── undo.py                 ← in-RAM 10-depth undo stack
│   ├── echo.py                 ← echo cancellation / barge-in guard
│   ├── wake_word.py            ← openwakeword wrapper
│   ├── hotkey.py               ← Ctrl+Space PTT (Win polling / Qt elsewhere)
│   ├── audio_devices.py        ← device picker + transport probe
│   ├── stt.py                  ← faster-whisper / vosk (UNUSED by main loop)
│   ├── tts.py                  ← Edge / Kokoro / ElevenLabs (UNUSED by main loop)
│   ├── llm_client.py           ← Ollama / OpenAI-compatible (UNUSED by main loop)
│   ├── installer.py            ← runtime pip for configured engines
│   ├── viseme.py               ← text→mouth-shape fusion
│   ├── avatar.py / avatar_mesh.py / face_model.obj   ← holographic head
│   └── prompt.txt              ← system prompt with {capabilities}/{limits} tokens
├── actions/                    ← 17 TOOL-dict files + 4 helpers (no TOOL)
│   ├── open_app.py, browser_control.py, computer_control.py,
│   ├── computer_settings.py, desktop.py, file_controller.py,
│   ├── file_processor.py, code_helper.py, dev_agent.py,
│   ├── reminder.py, send_message.py, video_player.py,
│   ├── youtube_video.py, weather_report.py, web_search.py,
│   ├── flight_finder.py, game_updater.py
│   └── (helpers) proactive.py, background_monitor.py,
│                 screen_processor.py, system_monitor.py
├── plugins/                    ← user-drop plugins (only _template.py present)
├── memory/
│   ├── memory_manager.py       ← long-term JSON memory + keyword search
│   ├── config_manager.py       ← user prefs persistence
│   └── long_term.json          ← currently near-empty
└── dashboard/
    ├── server.py               ← FastAPI phone remote (891 lines)
    └── static/                 ← app.html, login.html, crypto-js.min.js,
                                  jarvis-dashboard.html (static mockup)
```

**No `tests/`, no `pytest.ini`, no CI configuration.**

---

## 4. Actual Application Startup Flow

Verified from `main.py:2351-2365` and `JarvisLive.__init__`:

1. `main()` creates `JarvisUI("face.png")`, spawns a daemon runner thread, starts Qt event loop on main thread.
2. Runner thread blocks on `ui.wait_for_api_key()` — if `config/api_keys.json` has none, HUD prompts user.
3. `JarvisLive(ui)` constructor:
   - Discovers actions via `discover_actions(actions/, reserved={inline tool names})` → `self._action_registry`.
   - Discovers plugins via `discover_plugins(plugins/, core_tool_names=...)` → `self._plugin_registry`.
   - Wires UI callbacks (push_to_talk, text command, voice change, interrupt, remote key).
   - Loads wake-word preference; detector not yet started.
4. `asyncio.run(jarvis.run())` enters an infinite reconnect loop (`main.py:2102-2349`):
   - Binds confirm gate + memory trim notifier to UI.
   - Configures and prefetches audio devices.
   - Starts `DashboardServer` on a task (optional, swallows failures).
   - Picks the Live model (`_gemini.live_model()`), builds `LiveConnectConfig` from `_build_config()` (identity, time, memory, system prompt, tool declarations, session_resumption, sliding window, voice).
   - Opens `client.aio.live.connect(...)` and a `TaskGroup` with:
     - `_watch_reconnect`, `_send_realtime`, `_listen_audio`, `_receive_audio`, `_play_audio`, `_run_system_monitor`, `_run_background_monitor`, `_run_proactive_mode`, `_run_sleep_watch`, optional `_relay_phone_audio`, optional `_send_startup_briefing`.
5. On any exception the loop classifies it:
   - Reconnect signal → immediate retry.
   - Resumption handle rejected → drop handle, retry fresh.
   - Quota (`is_quota_error`) → cool model, step ladder.
   - Preview-API field rejected → drop `_tuned_live` or `_enhanced_live` and retry.
   - Invalid API key → prompt UI reconfig.
   - Network error → exponential backoff capped at 60s.
6. Session summary written if `len(_session_log) ≥ 3`.

Entry point: `python main.py` (also referenced from `readme.md` and `setup.py:124`).

---

## 5. Voice Pipeline

Actual flow:

```
┌─ sounddevice RawInputStream (16 kHz, int16, CHUNK=1024) ─ main.py:1309
│   callback(indata) — synchronous callback thread
│   ├─ Wake-word gate:   if sleeping → WakeWordDetector.feed(indata); return
│   ├─ Barge-in:         if JARVIS speaking → return (echo tail guard also here)
│   ├─ PTT gate:         if PTT on and not held → return
│   └─ else loop.call_soon_threadsafe(out_queue.put, {"data":..., "mime_type":"audio/pcm"})
│
├─ _send_realtime (main.py:1294) ──────────────────────────── Live API
│   session.send_realtime_input(audio=Blob(...))
│
└─ Google Gemini Live API
    │
    ├─ response.data             → audio_in_queue (sliced to 50ms)
    ├─ response.server_content   → output_transcription  → viseme stream + log + dashboard
    │                             → input_transcription   → log + _last_user_speech
    │                             → turn_complete         → flush buffers, close camera if pending
    ├─ response.tool_call        → _execute_tool(fc) foreach → session.send_tool_response(...)
    │                             → _flush_pending_vision() if a vision tool queued an image
    └─ session_resumption_update → cache resumable handle in RAM

Playback: _play_audio (main.py:1645)
  ├─ pulls from audio_in_queue, batches up to ~200ms
  ├─ computes visemes from PCM FFT → avatar mouth schedule
  ├─ stream.write (asyncio.to_thread) → RawOutputStream (24 kHz)
  └─ EchoGuard.note_output records what we played so the mic callback can subtract it
```

Failure points identified:
- Audio device open failure falls back to default (`main.py:1664-1674`) ✓
- `response.tool_call` runs serially per `function_calls` list; a single slow tool stalls audio-adjacent logic because `_receive_audio` awaits it inline (`main.py:1630-1639`).
- `_pending_vision` lives on `self`; a crash between queue and inject could leak `_vision_busy=True` until reconnect.
- Mic callback silently returns on any barge-in exception (`main.py:1361-1362`), which hides device problems.
- No end-to-end TTS: the voice IS Gemini Live. If Live is down the ladder (`core/gemini.py`) switches session to `gemini-2.5-flash-native-audio-preview-12-2025`; if both are rate-limited, the assistant cannot talk at all. `core/tts.py` exists but is **not wired into the main voice path**.

---

## 6. Gemini API Integration Analysis

Two integration surfaces:

**(a) Live session — the conversation.** Owned by `JarvisLive.run()` / `_build_config()`:
- `LiveConnectConfig` with `response_modalities=["AUDIO"]`, in+out transcription, system_instruction, tools, `session_resumption=SessionResumptionConfig(handle=self._resume_handle)`, `context_window_compression=ContextWindowCompressionConfig(sliding_window=...)`, speech voice config, optional `proactivity`/`realtime_input_config`/`media_resolution`/`thinking_config`.
- Session resumption handles captured from `response.session_resumption_update` when `resumable=True`; stored RAM-only.
- Explicit model fallback ladder in `core/gemini.py:162-165` (two Live models), with cooldowns:
  - Quota 5 min, gone/forbidden 6 h, unavailable 30 min.

**(b) One-shot calls — `core/gemini.py`.** Used by `web_search`, `code_helper`, `dev_agent`, `screen_find`, `flight_finder`, etc.:
- `FAST`/`SMART`/`SEARCH` tier ladders; each starts with `LIVE` throwaway session then REST models.
- `_live_call` spawns a dedicated thread + event loop; a semaphore caps concurrent Live sessions at **3** (`core/gemini.py:189`) so side calls can never starve the user's conversation.
- Timeouts: default **10 s** (API minimum). Callers can override; code and docs reviewers set 60+ s.
- JSON extraction uses `str.find('{')...rfind('}')` — brittle for nested braces or mixed JSON (`core/gemini.py:534-549`).

**Function calling / tool use** — Implemented. All tool declarations are concatenated in `_build_config` and passed as `tools=[{"function_declarations": _all_decls}]`. Results come back as `response.tool_call.function_calls` and are dispatched by `_execute_tool`.

**Context management** — `SlidingWindow` is relied on for truncation; no explicit token accounting, no summarizer at app layer. Session summary (end-of-session) is written via Gemini call (through `save_session_summary`).

**Weaknesses**
- No per-turn tool-call retry; a tool exception returns a string with `"failed: {e}"` and the model may invent a result rather than retry.
- No rate limiting on inline tool invocations — a model that calls `screen_process` repeatedly only hits the 4-second cooldown; other tools have no debounce.
- `_live_turn` sets `response_modalities=["AUDIO"]` for structured one-shot — relies on transcription fidelity. Documented as working but not validated by tests.
- Session resumption handle is explicitly RAM-only (`main.py:587-601`) by design; fresh launches lose context (session summary covers it partially).

---

## 7. Existing Skills and Tools Inventory

**Inline (defined in `main.py:331-498`, dispatched in `_execute_tool`):**

| Name | Purpose | Params | Verified result? | Permissions |
|---|---|---|---|---|
| `system_status` | CPU/RAM/GPU metrics | none | n/a | read-only |
| `screen_process` | capture screen or webcam, inject to session | `angle`, `text` | model answers from image | reads display/camera; 4s cooldown |
| `close_camera` | hide live cam view | none | n/a | UI only |
| `manage_monitor` | add/remove/list topic monitors | `action`, `topic` | returns string | writes memory |
| `shutdown_jarvis` | end process | none | n/a | **no confirm** |
| `save_memory` | write identity/pref/project/etc. | `category`, `key`, `value` | silent write | writes `long_term.json` |
| `recall_memory` | keyword search memory | `query` | returns dict | read-only |
| `undo` | pop and run undo stack | `action` | runs reverser | state-mutating |

**Auto-discovered actions (`actions/*.py` with `TOOL` dict):**

| Name | Source | Side effects (verified) |
|---|---|---|
| `browser_control` | 1132 LOC, Playwright | spawns browsers, filesystem profile dirs |
| `code_helper` | 633 LOC | writes files, runs interpreters, Gemini |
| `computer_control` | 589 LOC | pyautogui mouse/keyboard, screenshots, Gemini vision |
| `computer_settings` | 962 LOC | volume/brightness/wifi/dark mode/shortcuts; uses `confirm.request()` for restart/shutdown/wifi; pushes undo entries |
| `desktop_control` | 518 LOC | wallpaper, file org, **sandboxed exec of Gemini-generated code** |
| `dev_agent` | 639 LOC | multi-file project generation, runtime fix loop (5 attempts) |
| `file_controller` | 769 LOC | create/move/delete via send2trash, undo-aware |
| `file_processor` | 923 LOC | OCR/PDF/doc read, ffmpeg, Gemini summarization |
| `flight_finder` | 405 LOC | Gemini date parsing, URL compose |
| `game_updater` | 1101 LOC | Steam launch, update checks |
| `open_app` | 293 LOC | 70+ aliases × 3 OS, subprocess |
| `reminder` | 367 LOC | schedules background notifications |
| `send_message` | 348 LOC | Telegram/WhatsApp/Discord/Slack/email automation |
| `video_player` | 317 LOC | yt-dlp + Qt playback inside HUD |
| `youtube_video` | 475 LOC | search, play, transcript |
| `weather_report` | 71 LOC | webbrowser.open (Google search URL) |
| `web_search` | 416 LOC | Gemini grounding + DDG fallback, 15-min quota cooldown |

**Helper modules (no TOOL dict):**
- `background_monitor.py` — daily topic news polling; writes memory.
- `proactive.py` — unprompted suggestion engine (900 s silence, 1200 s cooldown).
- `screen_processor.py` — `_capture_screen` / `_capture_camera` (MSS/opencv, JPEG 82Q, 1280×720).
- `system_monitor.py` — CPU/RAM/GPU/temp with alert thresholds and 300 s re-trigger.

**Plugins:** only `plugins/_template.py` is present; no live plugins.

**Reuse / multi-step:** The architecture supports *adding* skills without touching main.py (confirmed), but there is **no composition framework** — tools are flat function-call declarations and the Live model chains them per turn implicitly.

---

## 8. Task Planning and Execution Capability Matrix

| Capability | Status | Evidence |
|---|---|---|
| Task decomposition / step planning | **Missing** | No planner module; relies on Gemini Live to chain function calls. |
| Tool selection on user intent | **Implemented** | Model-driven via tool declarations in system_instruction. |
| Multi-step workflow execution | **Partial** | Gemini can issue sequential tool calls per turn; no explicit workflow engine; `dev_agent` has its own 5-step fix loop. |
| Task-state tracking | **Missing** | No persistent task state; only `_session_log` (plain text) and transient `self._pending_vision`. |
| Result verification | **Missing** | Tool results returned verbatim; no second-model check. |
| Recovery from failed steps | **Partial** | Try/except → spoken error. Only `dev_agent` has a retry loop. Live reconnect ladder exists but is per-session not per-task. |
| Clarification on missing info | **Partial** | Prompted via `prompt.txt`'s "JUDGEMENT" section; relies on model behavior, not code. |
| Safe stopping / execution limits | **Partial** | `_vision_busy` cooldown; `confirm.py` for shutdown/restart/wifi; no global tool budget or kill switch. |

---

## 9. Memory and Conversation Context

**Short-term:** Live session's own context (sliding-window compressed). `_session_log` list in RAM for the summary.

**Long-term:** `memory/long_term.json` with 6 categories + `sessions` array. Public API in `memory/memory_manager.py`:

- `load_memory()` / `save_memory()` / `update_memory()` — merge + atomic write.
- `_trim_to_limit()` — activates > 200 KB on disk; drops oldest entries by `updated` date; notifies via `_trim_notifier`.
- `search_memory(query, limit=8)` — keyword scoring (key exact +10, key substr +6, value substr +3, category +1). **No embeddings, no vector index.**
- `format_memory_for_prompt()` — 3-tier: identity (always) + recent per-category (capped) + overflow key index.
- `save_session_summary()` — keeps last 3 only.

Config: `memory/config_manager.py` persists voice/wake/PTT/hud_style/thinking/turn_tuning/proactive_audio/media_resolution/brief/devices/plugin_config into `config/api_keys.json`.

**Current state:** `long_term.json` is empty scaffolding (verified).

**Privacy / secret handling:** No input scrubbing. Anything the Live model decides to call `save_memory` with lands in plaintext JSON. There is no deny list for secrets, no encryption at rest.

**Selective updates:** `update_memory` merges by category/key — safe. `forget(category, key)` deletes. There is no history/audit log.

---

## 10. File and Operating-System Access Analysis

**Mechanisms observed (file paths + behavior):**

| Mechanism | Where | Guard |
|---|---|---|
| Mouse / keyboard | `actions/computer_control.py` (pyautogui) | **None** |
| Shell / subprocess | `actions/open_app.py`, `computer_settings.py`, `desktop.py`, `game_updater.py`, `send_message.py` | List args (safe from shell injection); no confirm |
| Browser automation | `actions/browser_control.py` (Playwright) | **None** |
| File create/move/delete | `actions/file_controller.py` | send2trash; **undo stack** |
| Arbitrary code execution | `actions/code_helper.py`, `dev_agent.py`, `desktop.py` (sandbox blocks subprocess) | None for `code_helper`/`dev_agent`; `desktop.py` restricts subprocess inside generated code |
| Screenshots | `screen_processor.py`, `computer_control.py` | 4s cooldown on `screen_process`; others unrestricted |
| Camera | `screen_processor.py` | opens on demand |
| Volume / brightness / dark mode | `computer_settings.py` | **undo** entries pushed |
| Restart / shutdown / WiFi toggle | `computer_settings.py` | **confirm gate** (`core/confirm.py`), 90 s timeout |
| Reminders / scheduled scripts | `actions/reminder.py` (writes `.py` scripts to `~/.jarvis/reminders/`) | None |
| Phone remote command injection | `dashboard/server.py` → queue → Live model | AES-256-CBC with session PIN; no server-side command validation |

**Risks:**

- **No per-tool capability model.** The Live model's judgement is the only check for `computer_control.click`, keystroke, `browser_control.go_to`, `code_helper.run`, `file_controller.delete`.
- **Prompt injection via web_search / file_processor / screen_process:** untrusted content (web pages, PDFs, screenshots) can carry instructions that the model will try to execute via tool calls.
- **Reminder scripts** are generated Python written to `~/.jarvis/reminders/` and executed later — if the generation prompt is poisoned, so is the scheduled script.
- Shell commands in `computer_settings.py` use `osascript`, `powershell`, `pactl`, etc. with argument lists (verified safe from classical shell injection), but the *content* of arguments comes from the model.

---

## 11. Reliability and Error Handling Assessment

**Patterns observed:**

- Reconnect loop (`main.py:2138-2349`) catches `BaseException` and `BaseExceptionGroup`, classifies by substring:
  - `_ReconnectSignal`, resumption-rejected, quota, preview-field drift, invalid key, network.
  - Backoff: 3 s base, doubled on net error, capped at 60 s.
- `_execute_tool` wraps the whole body in try/except, returns `"Tool '{name}' failed: {e}"` and speaks an error.
- Each loader (actions / plugins) swallows per-file errors and logs; one bad action never aborts discovery.
- `confirm.py` has a 90 s timeout.
- `wake_word`, `audio_devices`, `tts`, `stt`, `installer` all catch broadly.

**Gaps:**
- No logging framework — everything is `print()` to stdout; `[JARVIS]` / `[Gemini]` / `[Dashboard]` prefixes only. No log rotation, no structured logs, no severity level, no file sink.
- No per-tool timeout. A `browser_control` session that hangs in Playwright blocks `_execute_tool` and starves the receive loop.
- No duplicate-call suppression apart from `screen_process` cooldown.
- `_pending_vision` can leak `_vision_busy=True` on an exception between capture and `_flush_pending_vision` — only recovers on next session reconnect.
- `_save_session_summary` runs via `create_task(...)` in a `finally` after the TaskGroup has exited; if the loop is about to be closed, it may be cancelled silently.
- Concurrency: `set_speaking_lock` is a `threading.Lock` used from an asyncio callback — contention is low but mixing paradigms is fragile.
- Graceful shutdown: `shutdown_jarvis` tool uses `os._exit(0)` after a 1.5 s goodbye — bypasses asyncio cleanup and daemon-thread finalizers.

**Highest-impact reliability gaps:**
1. No per-tool timeout → one hang stalls the entire pipeline.
2. No tests → any refactor is a regression bet.
3. All logs are prints → debugging a live session is reactive, not forensic.
4. Live session is the only talking path; one preview-field change at Google and voice stops.
5. Session summary isn't guaranteed to run on crash.

---

## 12. Security and Privacy Findings

| Severity | Finding | Evidence | Consequence | Recommendation | Validation |
|---|---|---|---|---|---|
| **Critical** | Gemini API key stored plaintext | `config/api_keys.json` | Key leak via repo clone, backup, screen share | Use OS keychain (keyring lib) or at minimum DPAPI/Keychain-wrapped file; move to `~/.jarvis/`, out of repo | grep repo for key value; confirm chmod 600 |
| **High** | No confirmation on destructive tools | `file_controller.delete` uses send2trash (recoverable), but `computer_control.click/type/hotkey`, `browser_control`, `code_helper.run`, `dev_agent` all run without a gate | Prompt-injected content can drive the mouse/keyboard | Extend `confirm.py` to a per-tool policy (`safe` / `confirm` / `block`); default `code_helper.run`, `dev_agent` runs, outbound `send_message` to `confirm` | mock model that calls a destructive tool mid-session |
| **High** | Model-generated code is executed | `code_helper`, `dev_agent`, `desktop_control` sandbox | Supply-chain / prompt-injection path to arbitrary code | Run inside a subprocess with resource limits + no network; show diff before run | run with poisoned description and verify block |
| **High** | Reminders write and run arbitrary .py | `actions/reminder.py` writes to `~/.jarvis/reminders/` | Delayed arbitrary execution | Replace with declarative schedule store + fixed executor | inspect generated files |
| **High** | Prompt-injection surface via web/screen/file inputs | `web_search`, `file_processor`, `screen_process`, `youtube_video.transcript` | Attacker-controlled text can issue tool calls | Treat tool outputs as untrusted; strip "ignore previous" patterns; forbid tool-reasoning from pure untrusted-content turns | fuzz with crafted PDFs/pages |
| **Medium** | Dashboard self-signed cert never pinned | `dashboard/server.py` | MITM on local network can read PIN exchange | Pin fingerprint in the mobile client; or require manual trust confirmation each session | wiretap with mitmproxy |
| **Medium** | Upload endpoint accepts any extension | `dashboard/server.py:/api/upload` (500 MB limit) | User can upload .exe/.py; later tool invocation could execute | Extension / MIME allowlist | upload `.py` and watch for execution path |
| **Medium** | No PII filter on `save_memory` | `memory_manager.remember` | Credentials spoken by user land in plaintext JSON | Regex scrub (CC, token, password), optional encryption at rest | call with fake creds |
| **Medium** | `_tokens` set in dashboard is in-RAM only | `dashboard/server.py` | Fine for one process; but no rate-limit on `/login` POST | Add per-IP backoff on failed PIN | brute force PIN locally |
| **Low** | No CORS configuration | `dashboard/server.py` | Default FastAPI (no CORS headers) — browser same-origin is the only guard | Explicit CORS allowlist | fetch from other origin and confirm block |
| **Low** | `_get_api_key` reads JSON each connect | `main.py:293-295` | File race w/ UI write path | Lock around write/read | parallel UI save during reconnect |
| **Low** | `shutdown_jarvis` uses `os._exit(0)` | `main.py:1239` | Skips finalizers, may truncate session summary | Set an event, await task group exit | force shutdown and inspect `long_term.json` |

**No secret values are reproduced in this report.** The presence and location of the plaintext key is documented; the value is not.

---

## 13. Existing Testing and Quality Assessment

- `find`-confirmed: **zero** `test_*.py`, zero `*_test.py`, no `tests/` directory, no `pytest.ini`, no `conftest.py`, no CI config in the repo.
- Unit coverage: **unmeasured** — no tool present to measure.
- Integration tests: **none**.
- Voice-pipeline tests: **none** (readme claims "measured 0.24 s connect" etc. but these are developer measurements, not committed tests).
- Error-path tests: **none**.
- Static analysis: no `ruff`, `mypy`, `pyright` config.

**Code-quality observations:**
- `main.py` is one 2365-line file with a single `JarvisLive` class — hard to test in isolation.
- `ui.py` is 6591 lines, not reviewed in depth, but by LOC alone it is a maintainability concern.
- Tool declarations are duplicated: once in Python TOOL dict, once (implicitly) in the model's view of the prompt. Keeping these aligned is manual.
- Docstrings and comments are thorough — rare for a Python app this size — and read as production narrative (seen throughout `core/gemini.py`, `main.py:1073`, `core/action_loader.py`).

---

## 14. Top 15 Improvement Opportunities (ranked)

1. **Per-tool timeout + watchdog** — wrap every `_execute_tool` dispatch in `asyncio.wait_for(...)`; abort and speak a controlled failure. (High impact / Low effort.)
2. **Permission model** — classify every tool as `safe`/`confirm`/`destructive`/`network-out`; route through `confirm.py` by default for `destructive` and outbound messages. (High / Medium.)
3. **Secret storage** — move API key out of plaintext JSON into OS keyring. (High / Low.)
4. **Automated test harness** — pytest with fixtures that mock `genai.Client`, record+replay tool-call traces. (High / Medium.)
5. **Structured logging + log file** — replace `print` with `logging` + rotating file; add correlation IDs per tool call. (High / Low.)
6. **Planner module** — add a lightweight planner that, for "complex" intents, writes a stepwise plan to a `TaskState` and executes tools one at a time with verification between steps. (High / High.)
7. **Result verifier** — a cheap FAST-tier call per tool result that scores "does this satisfy the user's intent"; feed back to planner on fail. (Medium / Medium.)
8. **Prompt-injection defence** — label every string returned from `web_search`, `file_processor`, `screen_process` as untrusted; the planner must not act on new tool calls derived purely from untrusted content without user confirmation. (High / Medium.)
9. **Persistent memory upgrade** — add embedding index (sqlite-vss or sqlite-vec) alongside keyword search; keep the JSON file as source-of-truth. (Medium / Medium.)
10. **Session resumption persistence (opt-in)** — persist the resumption handle across restarts behind a flag so long-running tasks survive crashes. Decide carefully against the "session summary on next launch" tradeoff documented in `main.py:587-601`. (Medium / Low.)
11. **Split `main.py`** — carve `JarvisLive` into: `session_runner.py`, `tool_dispatcher.py`, `voice_pipeline.py`, `vision_capture.py`, `briefing.py`. (High / High.)
12. **Reminder subsystem** — stop writing executable `.py`; store declarative reminders in JSON and run them through a fixed executor. (Medium / Low.)
13. **Dashboard hardening** — rate-limit `/login`, enforce upload MIME allowlist, pin cert fingerprint, add CORS allowlist. (Medium / Low.)
14. **Observability hooks** — emit tool-call metrics (name, latency, success) to a local SQLite; expose to the dashboard's Analytics tab (currently a static mockup). (Medium / Medium.)
15. **Tool-call budget per turn** — cap the number of tool calls per model turn to prevent runaway loops; log and refuse past the cap. (Medium / Low.)

---

## 15. Recommended Target Architecture (recommendation, not existing)

```
┌─────────────────────────────────────────────────────────┐
│ user voice / phone / text                               │
└──────────────┬──────────────────────────────────────────┘
               ▼
     ┌──────────────────┐         ┌───────────────────────┐
     │ Voice front end  │ ◀────── │ TTS fallback engines  │
     │ (Gemini Live)    │         │ (Edge/Kokoro/11L)     │
     └────┬─────────────┘         └───────────────────────┘
          ▼
┌──────────────────────────────────────────────────────────┐
│ Intent router                                            │
│   if simple → direct tool call                           │
│   if complex → Planner                                   │
└────┬─────────────────────────────────────────────────────┘
     ▼
┌──────────────────────────────────────────────────────────┐
│ Planner (new)                                            │
│   step list stored in TaskState (sqlite)                 │
└────┬─────────────────────────────────────────────────────┘
     ▼
┌──────────────────────────────────────────────────────────┐
│ Tool Dispatcher (new, out of main.py)                    │
│   permission check → per-tool timeout → execution        │
│   result wrapped in {trust: trusted|untrusted}           │
└────┬─────────────────────────────────────────────────────┘
     ▼
┌──────────────────────────────────────────────────────────┐
│ Verifier (new, FAST tier)                                │
│   satisfied? → next step | retry | ask-user              │
└────┬─────────────────────────────────────────────────────┘
     ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌────────┐
│ Skill       │ │ Skill       │ │ Skill       │ │ Plugin │
│ Registry    │ │ Registry    │ │ Registry    │ │ Reg.   │
│ (OS)        │ │ (Web)       │ │ (Media)     │ │        │
└─────────────┘ └─────────────┘ └─────────────┘ └────────┘

Cross-cutting:
  • Secret store (OS keyring)
  • Structured logging + metrics (sqlite)
  • Memory: JSON + vector index
  • Confirm gate + undo stack (reuse existing)
  • Observability dashboard (populated, not mocked)
```

Keep the Live API for the user-facing conversation. Move all one-shot work through `core/gemini.py` (already correct). Keep the TOOL-dict discovery contract — it is good and already matches an MCP-style function registry.

---

## 16. Proposed Implementation Phases and Dependencies

**Phase 0 — Hygiene (blocks everything else)**
- Secret storage (keyring).
- Logging framework + file sink.
- Pytest harness with Gemini mock.
- Dependencies: none.

**Phase 1 — Reliability**
- Per-tool timeout + dispatcher refactor.
- Watchdog for stuck vision/tool state.
- Session summary guaranteed on crash.
- Depends on: Phase 0 logging.

**Phase 2 — Safety**
- Permission model wired to `confirm.py`.
- Trust-tagged tool outputs.
- Reminder-system rewrite (no generated .py).
- Depends on: Phase 1 dispatcher refactor.

**Phase 3 — Planning & verification**
- `TaskState` sqlite store.
- Planner module (small Gemini SMART-tier calls).
- Verifier module (FAST-tier).
- Tool-call budget.
- Depends on: Phase 1 (dispatcher) + Phase 2 (trust tags).

**Phase 4 — Memory upgrade**
- Embeddings (sqlite-vec or chroma); dual-write with keyword store.
- PII scrubbing before `save_memory`.
- Depends on: Phase 0 (logging) only.

**Phase 5 — Dashboard & observability**
- Metrics sink from dispatcher → dashboard Analytics tab.
- Dashboard hardening (rate limit, MIME allowlist, cert pin).
- Depends on: Phase 1 logging / metrics.

**Phase 6 — Modularisation**
- Split `main.py` and `ui.py` along the component boundaries drawn in §15.
- Depends on: all prior phases (tests give safety for the split).

---

## 17. Risks, Unknowns, and Questions

**Unknowns I could not confirm from static read-only review:**
- Behaviour of `ui.py` (6591 LOC) under failure — only its public attributes used by main were inferred.
- Whether `core/tts.py`, `core/stt.py`, `core/llm_client.py` are reached by any plugin at runtime (grepped only; no plugins installed).
- Whether all 17 actions actually work on this machine (not executed per the audit rules).
- Whether `openwakeword` is installed in the running env (it's opt-in).
- Full details of `send_message` cross-platform message delivery mechanics beyond file header read.
- Full details of `file_controller`, `file_processor`, `flight_finder`, `game_updater`, `open_app`, `reminder`, `send_message`, `video_player`, `youtube_video`, `web_search` — only summaries from subagent reads.

**Questions for you:**
1. **Keep Gemini Live as the sole voice path**, or add offline TTS/STT fallback for air-gapped use?
2. **Memory privacy stance** — is any plaintext memory acceptable, or do you want everything at rest encrypted?
3. **Confirmation model** — do you want per-tool confirm, or an "autopilot vs. supervised" global mode toggle?
4. **Planner budget** — how many extra Gemini calls per complex task are acceptable (verifier doubles call count)?
5. **Reminder redesign** — can you drop the "generate a .py script" behaviour entirely?
6. **Session persistence** — do you want the resumption handle persisted across restarts (lose morning briefing continuity) or kept RAM-only?
7. **Target platforms** — do we optimise for mac (your current OS per `api_keys.json`), or must Windows/Linux parity stay?
8. **Dashboard scope** — is the phone remote in-scope for the upgrade or frozen for now?

---

## 18. Final Audit Checklist

| Area | Status |
|---|---|
| Project structure, entry points | **Complete** |
| Dependencies & setup | **Complete** |
| Startup flow | **Complete** |
| Voice pipeline tracing | **Complete** |
| Gemini Live integration | **Complete** |
| Gemini one-shot ladder | **Complete** |
| Inline tool handlers in `main.py` | **Complete** |
| Action auto-discovery contract | **Complete** |
| Plugin auto-discovery contract | **Complete** |
| Action inventory (file-level) | **Complete for 17 action files + 4 helpers** |
| Action deep internals | **Partial** — browser_control, computer_control, computer_settings, code_helper, dev_agent, desktop reviewed closest; file_controller, file_processor, flight_finder, game_updater, open_app, reminder, send_message, video_player, youtube_video, web_search summarised only |
| Memory manager | **Complete** |
| Config manager | **Complete** |
| `prompt.txt` structure | **Partial** — first ~80 lines read, remainder not inspected |
| Dashboard server endpoints | **Complete** |
| Dashboard static UI | **Surface** — only `jarvis-dashboard.html` sketched; `app.html` / `login.html` not inspected |
| `ui.py` deep review | **Not inspected** (6591 LOC, out of scope for this pass) |
| `core/avatar.py`, `avatar_mesh.py` | **Surface** (functionality confirmed) |
| `core/tts.py`, `core/stt.py`, `core/llm_client.py` | **Surface** — present but not used by the live voice path |
| Tests | **Verified absent** |
| CI / linting | **Verified absent** |
| Credentials/secrets scan | **Complete — one plaintext key flagged, not reproduced** |

**No files were modified, created, deleted, renamed, or executed. No commands with side effects were run. No credentials, tokens, or secret values are reproduced in this report.**

Ready for approval before any implementation plan.
