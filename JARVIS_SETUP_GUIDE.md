# JARVIS Setup Guide

Short operator doc covering runtime toggles added during the current upgrade cycle. For first-time installation, see `readme.md`.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `JARVIS_LOG_LEVEL` | `INFO` | Root log level. Values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Applied at process start. |
| `JARVIS_CPU_CEILING` *(reserved, P1.4)* | `85` | Worker defers dequeuing new steps while CPU% exceeds this value. |

## Logs

- Structured JSON logs land at `logs/jarvis.log` with 5 × 10 MB rotation.
- Trace IDs are attached via `with_trace_id()` in `core/logging_setup.py`; look for `"trace_id":"abc123..."` to follow a request end-to-end.

## Dashboard

- Default port: `8000` (HTTP; HTTPS auto-enabled when `config/certs/jarvis.{key,crt}` exist).
- Rate limits: `/login` 10 burst / 1 req s⁻¹ per IP; `/api/*` 60 burst / 10 req s⁻¹ per IP. Exceeding returns HTTP 429.
- Health probe: `curl http://localhost:8000/health` → `{"status":"ok","uptime_s":...,"clients":...,"tokens":...}`.

## Emergency stop *(reserved, P2.6)*

Three triggers:
1. Create a flag file: `touch ~/.jarvis_killswitch`.
2. Global hotkey (default binding configurable in `config/hotkey.json`).
3. Dashboard button posting `/api/killswitch`.

Effect: cancels all running tasks, closes Playwright browser contexts, interrupts TTS mid-utterance, writes a final journal event.

## Credentials

All third-party credentials live in the system keyring via `core/secrets.py`. Direct editing of `config/api_keys.json` is discouraged. To set a key:

```python
from core.secrets import set_api_key
set_api_key("gemini_api_key", "sk-…")
```

Never commit `config/api_keys.json` or any plaintext key.

## Optional dependencies

Several P2+ features require optional packages. Installation is only needed when the feature is used; all guards are `try: import …` and return informative errors otherwise.

| Feature | Package |
|---|---|
| PDF generation (P2.3) | `reportlab` |
| File watcher (P2.5) | `watchdog` |
| Scheduled automation (P2.2) | stdlib — no extra dep |
| WhatsApp (P4.1) | `twilio` |
| Telegram (P4.2) | `python-telegram-bot` |
| Gmail / GCal / GDrive (P4.3–5) | `google-api-python-client`, `google-auth-oauthlib` |
| Microsoft 365 (P4.6) | `msal` + `msgraph-core` |
| Voice auth (P4.8) | `speechbrain` or `resemblyzer` |

## Running tests

```bash
python3 -m pytest           # full suite (baseline: 525 green)
python3 -m pytest -v tests/test_logging_setup.py
```
