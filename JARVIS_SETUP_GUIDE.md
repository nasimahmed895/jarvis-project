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

## Browser automation

JARVIS can drive a real browser end-to-end. The engine is Playwright plus,
on macOS Chromium-family browsers, AppleScript — so the assistant controls
the browser the user is already signed into instead of an isolated sandbox.

### One-time setup

```bash
pip install -r requirements.txt
python3 -m playwright install chromium   # browser binary for headless jobs
# macOS Chrome/Brave/Edge only: enable "Allow JavaScript from Apple Events"
#   View → Developer → Allow JavaScript from Apple Events  (or: action="enable_js")
```

### Tool surface (Gemini Live sees)

| Tool | Purpose | Approval level |
|---|---|---|
| `browser_control` | Low-level verbs: `go_to`, `search`, `click`, `type`, `scroll`, `screenshot`, `get_text`, `fill_form`, tabs, `smart_click`, `smart_type` | LVL1 for reads/nav, LVL2 for form submits, LVL3 for `start_debug` |
| `web_search` | Google / Bing / DDG queries with compare and research modes | LVL1 |
| `jobs` | Listing search, resume matching, cover-letter drafting, gated apply | LVL3 on `apply` |
| `real_estate` | Property portals, side-by-side compare, gated inquiries | LVL2 on `inquire` |
| `education` | Courses / papers / scholarships / study plans, gated enrol | LVL3 on `enroll` |
| `customer_support` | KB search, ticket drafting, thread summarisation, gated portal submit | LVL2 on `open_ticket` |
| `social_media` | Draft post sized to the platform, hashtag suggestions, gated publish | LVL2 on `publish` |
| `travel`, `flight_finder`, `youtube_video`, `morning_briefing`, `email_manager`, `calendar_manager`, `crm`, `market_research`, `meeting_prep`, `report_gen`, `grounded`, `research` | Pre-existing module coverage | per-module |

A full multi-step goal is handled by `core/planner.py` → `core/worker.py`:
the planner asks Gemini for a JSON step list, the worker dispatches each
step, verifies, and resumes across restarts.

### Approval model

`core/approval.py` classifies every action into three levels:

- **LVL1 — read-only / reversible.** Runs without prompting (searches, page
  reads, navigation, screenshots, downloads to a local folder).
- **LVL2 — mutating but recoverable.** Asks on the HUD unless the user has
  pre-approved the category via `config/approval.json`. Categories include
  `email_draft`, `calendar_write`, `social_post`, `form_submit`,
  `upload_file`, `crm_write`.
- **LVL3 — irreversible or financial.** Always routed through
  `core/confirm.py` — the HUD banner issues the confirmation token; the
  model never does. Examples: payments, shutdown/restart, delete-file,
  send-email, submit-job-application, start-debug-browser.

LVL3 cannot be bypassed from the model side. If the HUD is not bound
(headless or test context), LVL3 refuses rather than running.

### Example commands (voice or text)

```
Jarvis, find five Python remote jobs under three hundred a day and rank them against my resume.
Jarvis, search Zillow for a two-bed in Lisbon for rent, compare the best three.
Jarvis, draft a cover letter for the LinkedIn listing currently on screen.
Jarvis, draft a LinkedIn post about today's release and suggest five hashtags.
Jarvis, prepare a support ticket for the WidgetCo portal — login fails on Android after 2.4.1.
Jarvis, build me a four-week Rust study plan for six hours a week.
Jarvis, open my inbox and summarise anything from last night marked urgent.
Jarvis, search for PhD scholarships in machine learning in Europe.
```

### Dashboard

`/api/data/browser` (GET) returns tasks whose plans touch browser-adjacent
tools, the latest screenshot on disk, and the currently-running task's
step list. `/api/data/browser/stop` (POST) cancels all running
browser-touching tasks — running steps complete; the worker stops
dispatching further ones.

## Predictive intelligence

The predictive stack sits on top of the project's long-running stores
(tasks, schedules, journal) and adds a ledger that *scores itself* over
time. Design priorities: evidence-based, explainable, calibrated.

### Layout

| Module | Role |
|---|---|
| `core/predictor.py` | Distils recurring-slot patterns from the journal. Heuristic (median + spread + support ≥3). Returns suggestions. |
| `core/predictions.py` | Ledger. `record()` on prediction, `resolve()` on outcome. SQLite at `memory/predictions.sqlite`. Computes MAE / RMSE / MAPE / Brier + calibration buckets. |
| `core/risk.py` | Aggregates explainable risk items from tasks / schedules / prediction accuracy. Rule-based with full evidence; every rule can be inspected and replaced. |
| `core/scenarios.py` | Pure what-if arithmetic: `percent_change`, `shift_deadline`, `delay_task`, `demand_change`, `traffic_surge`. Returns `{expected, optimistic, pessimistic, assumptions}`. |
| `actions/forecast.py` | Gemini tool exposing `suggest / risks / scenario / record / resolve / metrics / list`. |

### Flow

```
+----------------+  record()   +-----------------+
| model forecast | ----------> | predictions.sq  |
+----------------+             +-----------------+
         |                              ^
         | outcome known                |  resolve(id, actual)
         v                              |
+----------------+                      |
|  user / event  |----------------------+
+----------------+
```

The assistant is encouraged (via `actions/forecast.py` description) to
call `record` whenever it produces a forecast and `resolve` when an
outcome lands. The ledger then exposes real calibration through
`metrics()`.

### Confidence policy

The model's self-reported confidence is stored but never promoted to
"accuracy". Calibration metrics (Brier, reliability buckets) are the
honest signal. The dashboard displays the ledger's observed mean actual
per predicted-probability bucket — if the assistant systematically says
"80% confident" but is right only 55% of the time, the operator sees it.

### Risk rules

Shipped rules:

- **overdue_tasks** — running tasks without updates for > 24h (medium) / 72h (high).
- **awaiting_user** — tasks waiting on the HUD banner for > 6h.
- **schedule_conflicts** — two scheduled items firing within 2 min.
- **failed_predictions** — a prediction category with MAPE > 50% over ≥5 resolved samples.

Custom rules register via `core.risk.register_rule(fn)`. A rule that
raises is caught and surfaced as a `low`-severity infra risk — a broken
rule does not sink the report.

### Dashboard endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`   | `/api/data/predictions` | ledger + metrics, filterable by category/status |
| `POST`  | `/api/data/predictions/resolve` | record outcome `{id, actual \| actual_label}` |
| `GET`   | `/api/data/risks` | current risk register |
| `POST`  | `/api/data/scenarios` | compute what-if `{shape, …inputs}` |
| `POST`  | `/api/data/memory` | upsert `{category, key, value}` |
| `DELETE` | `/api/data/memory` | delete `{category, key}` |

### Example voice commands

```
Jarvis, what are my risks right now?
Jarvis, if I delay the launch by three days, what's the knock-on?
Jarvis, what did my last ten spend forecasts actually turn into?
Jarvis, record a prediction: project X ships Nov 25, confidence 60 percent.
Jarvis, forecast: what if traffic doubles and capacity is a thousand RPS?
```

## Profile-driven form filling and site credentials

Two additions turn `browser_control` into a genuine form agent:

### `memory/user_profile.py`

A typed, flat JSON store at `memory/user_profile.json` with canonical
fields (first_name, last_name, email, phone, address_line1, …), synonyms
for label matching, and a `missing(required)` helper. `form_fill` uses it
to know *what information exists*, so the assistant asks for a specific
missing value instead of refusing the whole task.

```python
from memory import user_profile
user_profile.set("first_name", "Ada")
user_profile.set("email", "ada@example.com")
user_profile.missing(["first_name", "last_name", "email"])
# → ['last_name']
```

### `actions/form_fill.py` (Gemini tool)

```
form_fill(kind="registration", url="https://example.test/signup",
          submit_after=True, submit_label="Create account")
```

Flow: navigate → match the profile fields against the detected form →
delegate to `browser_control.fill_form` → gate the submit through
`core/approval.py` (LVL2 `form_submit` category).

Supported kinds: `registration | application | contact | profile | login |
generic`. Default required sets are conservative; pass `required_extra`
to tighten per-site.

### `actions/credential.py` (Gemini tool)

Site credentials live in the OS keyring with an AES-encrypted disk
fallback (`config/site_creds.enc`). The password is never returned in a
tool result or chat transcript — only a success / failure line plus a
scrubbed summary of the fill and click.

```
credential(action="save",  host="github.com",
           username="me@example.com", password="…")
credential(action="login", host="github.com")         # LVL2 gated
credential(action="list_hosts")
credential(action="forget", host="github.com")
```

MFA / CAPTCHA flows always hand control back to the user.

## Prompt-level refusal fix

`core/prompt.txt` now includes an explicit anti-refusal clause inside
the `[BROWSER_AGENT]` section: *signing in to the user's own account,
filling a registration or application form with information the user has
given, submitting a form after the user has reviewed it* are things this
build is designed to do, and the correct response is to open the page
and act — NOT "I cannot do that on your behalf". When a required piece
of profile data is genuinely missing, the assistant asks for *that
specific value* rather than refusing the whole task.

## Browser automation fixes (session 4)

Targeted fixes to the real bugs surfaced in the conversation logs. The
2170-line `actions/browser_control.py` is otherwise unchanged; only the
specific unsafe paths were edited.

### Root causes addressed

| Symptom (from logs) | Root cause | Fix |
|---|---|---|
| `BrowserContext.new_page: Target page, context or browser has been closed` | 3 unguarded calls to `self._context.new_page()` | New `_ctx_alive()` helper. `_get_page`, `_adopt_page`, `go_to`, `new_tab` now check it and raise a clear error instead of crashing. |
| Lost active page after clicking | `close_tab` assigned `pages[-1]` even when that page was closed; `switch_tab` adopted a target that could close between filter and assignment | New `_first_live_page()` helper. `close_tab` / `switch_tab` always leave `self._page` on a live sibling or None. |
| "Fill succeeded" when nothing was filled | `fill_form` typed, never read values back | `fill_form` now reads `locator.input_value()` and reports `N/M verified` with every value that failed to round-trip. |
| Only matched exact English labels ("Sign Up") | `click` used `get_by_text(text, exact=False)` only | `click` now tries `get_by_role` first (button/link/menuitem/tab/checkbox/radio), then label / placeholder / exact-text / substring-text / aria-attrs. Synonyms from `core/synonyms.py` expand intent keywords to English + Bengali surface forms. |
| Passwords echoed in fill summaries | No scrubbing | Keys containing `password/secret/pin/otp/token/cvv/cvc/card` and inputs with `type=password` emit `(masked)` in all logged / returned strings. |

### `core/synonyms.py`

Intent → surface forms for form-level vocabulary. Covers English and
Bengali for: register, login, logout, submit, next, cancel, search,
full_name, first_name, last_name, email, phone, password,
confirm_password, shop_name, address, city, state, postal_code, country,
date_of_birth, gender, username, otp.

```python
from core import synonyms
synonyms.expand("register")
# → ['Register', 'Sign Up', 'Create Account', …, 'নিবন্ধন', 'একাউন্ট তৈরি করুন', …]
synonyms.intent_for("Please enter your mobile number")   # → 'phone'
synonyms.intent_for("ইমেইল ঠিকানা")                        # → 'email'
```

### Click strategy order

1. `get_by_role` (button, link, menuitem, tab, checkbox, radio)
2. `get_by_label`
3. `get_by_placeholder`
4. `get_by_text(exact=True)`
5. `get_by_text(exact=False)`
6. CSS attribute match (`aria-label`, `title`, `alt`)

Applied once with the raw `text`, then re-applied with each synonym.
Return value names the strategy that matched so the assistant knows
what worked, e.g. `Clicked by role=button name='Sign Up' (expanded
from 'register')`.

### Fill verification contract

Every `fill_form(fields)` return now has the shape:

```
Fill summary: 3/4 verified.
Filled & verified: email→Email [role=textbox] = ada@example.com; full_name→Full Name [role=textbox] = Ada L; password→password [role=textbox] = (masked)
Mismatch (value did not round-trip): phone wanted=+100... got=(empty)
```

The model is instructed never to claim success unless the summary shows
`N/N verified` AND the mismatch section is empty.

### Limitations and non-goals

- CAPTCHAs, MFA codes, identity challenges: **always** handed back to the
  user. The engine never attempts to solve them and never relays them to a
  third party.
- Payments, money transfers, legal / tax / government submissions: LVL3 —
  the user must press CONFIRM on the HUD banner.
- Platform rule evasion (follow-for-follow, mass DM, fake engagement,
  scraping behind authentication the user did not authorise): refused at
  the prompt layer in the relevant module.
- Secrets never land in logs or the dashboard. See `core/secrets.py` for
  credential storage.
