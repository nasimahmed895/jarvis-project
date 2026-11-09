"""
core/ai_adapters.py — per-site configuration for AI websites.

The glue that actually DRIVES the page lives in `actions/ai_site.py`,
which talks to `actions/browser_control.py` (go_to, fill_form, click,
inspect_form, get_text, verify_page) through the existing session
registry. The adapters here are *data* — they tell the glue which URLs
to open, which locators to try for composer / send-button /
response-container, and how to decide the model is done answering.

Why data-driven, not per-site classes
-------------------------------------
AI sites change their DOM constantly. A class hierarchy would make each
change a code change; a flat Adapter dataclass makes it an attribute
edit. Multiple locator candidates per role (composer, send button,
response) let the first-match-wins strategy survive one of them going
stale. Adding a fourth site is a new entry in `_ADAPTERS`, no logic
change.

What this file DOES NOT do
--------------------------
* No DOM calls — those are `browser_control` territory.
* No auth. Site login stays the user's responsibility.
* No CAPTCHA / MFA bypass — never has been, never will be.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class Adapter:
    """One AI website's shape. All selector lists are tried in order;
    first match wins. Selector syntax is `browser_control.fill_form`'s —
    plain label text works via role=textbox / get_by_label, and a raw
    CSS selector (string starting with `#`, `.`, `[`, or containing a
    `>` / `:`) is tried last.
    """
    key: str                   # "chatgpt", "claude", "gemini"
    name: str                  # Human name
    url: str                   # Landing URL (opens a new conversation)
    url_hosts: tuple[str, ...] # Hostnames considered "this site"

    # Composer locators — any of these should match the message input.
    composer: tuple[str, ...]

    # Send-button text labels (synonyms expanded per-locale).
    send_labels: tuple[str, ...]

    # Last-response container selector. First match wins.
    response_selector: tuple[str, ...]

    # "Still generating" indicator: a visible element whose presence
    # means the model has NOT finished. When none of these are present,
    # the answer is treated as complete.
    busy_selector: tuple[str, ...]

    # New-chat trigger label(s).
    new_chat_labels: tuple[str, ...] = ()

    # Optional file-upload trigger label(s).
    upload_labels: tuple[str, ...] = ()

    # Hard cap on how long we'll poll for a response (seconds).
    response_timeout_s: int = 180

    def host_match(self, url: str) -> bool:
        if not url:
            return False
        low = url.lower()
        return any(h in low for h in self.url_hosts)


# ── Shipped adapters ────────────────────────────────────────────────

_CHATGPT = Adapter(
    key="chatgpt",
    name="ChatGPT",
    url="https://chat.openai.com/",
    url_hosts=("chat.openai.com", "chatgpt.com"),
    composer=(
        "Message ChatGPT",             # placeholder (common)
        "Ask anything",                # newer placeholder
        "Send a message",              # legacy
        "#prompt-textarea",            # id (frequent)
        "textarea[data-id='root']",    # fallback CSS
        "div[contenteditable='true']", # React-controlled composer
    ),
    send_labels=("Send message", "Send", "Submit"),
    response_selector=(
        "[data-message-author-role='assistant']:last-of-type",
        ".markdown:last-of-type",
        "div[data-message-id]:last-of-type",
    ),
    busy_selector=(
        "[data-testid='stop-button']",
        "button[aria-label='Stop generating']",
        "button[aria-label*='Stop']",
    ),
    new_chat_labels=("New chat", "New Chat"),
    upload_labels=("Attach files", "Attach", "Upload"),
)

_CLAUDE = Adapter(
    key="claude",
    name="Claude",
    url="https://claude.ai/new",
    url_hosts=("claude.ai",),
    composer=(
        "Reply to Claude",             # placeholder
        "How can I help you today?",   # placeholder (landing)
        "Message Claude",
        "div[contenteditable='true']",
        "div[role='textbox']",
    ),
    send_labels=("Send Message", "Send", "Submit"),
    response_selector=(
        "div[data-is-streaming='false']:last-of-type",
        "[data-testid='claude-message']:last-of-type",
        ".prose:last-of-type",
    ),
    busy_selector=(
        "[data-is-streaming='true']",
        "button[aria-label='Stop response']",
    ),
    new_chat_labels=("New chat", "Start new chat"),
    upload_labels=("Attach", "Add content", "Upload file"),
)

_GEMINI = Adapter(
    key="gemini",
    name="Gemini",
    url="https://gemini.google.com/app",
    url_hosts=("gemini.google.com", "bard.google.com"),
    composer=(
        "Enter a prompt here",
        "Ask Gemini",
        "rich-textarea",
        "div[contenteditable='true']",
    ),
    send_labels=("Send message", "Submit", "Run"),
    response_selector=(
        "message-content:last-of-type",
        "[data-response-chunk]:last-of-type",
        ".response-container-content:last-of-type",
    ),
    busy_selector=(
        "button[aria-label='Stop response']",
        ".loading-spinner",
        "[data-test-id='stop-generating']",
    ),
    new_chat_labels=("New chat", "Reset chat"),
    upload_labels=("Add files", "Upload image"),
)


_ADAPTERS: dict[str, Adapter] = {
    _CHATGPT.key: _CHATGPT,
    _CLAUDE.key:  _CLAUDE,
    _GEMINI.key:  _GEMINI,
}

# Canonical aliases people say / write.
_ALIASES: dict[str, str] = {
    "gpt":        "chatgpt",
    "chat-gpt":   "chatgpt",
    "openai":     "chatgpt",
    "anthropic":  "claude",
    "google ai":  "gemini",
    "bard":       "gemini",
}


# ── Public API ──────────────────────────────────────────────────────

def available() -> Iterable[str]:
    return _ADAPTERS.keys()


def get(name: str) -> Adapter | None:
    if not name:
        return None
    key = name.lower().strip()
    key = _ALIASES.get(key, key)
    return _ADAPTERS.get(key)


def register(adapter: Adapter) -> None:
    """Allow plugins to add a new AI site at runtime."""
    _ADAPTERS[adapter.key] = adapter


def detect_from_url(url: str) -> Adapter | None:
    """Reverse-lookup: given the active tab's URL, which adapter owns
    it? Used so a user who already has Claude open doesn't have to say
    'open Claude' again."""
    if not url:
        return None
    for a in _ADAPTERS.values():
        if a.host_match(url):
            return a
    return None
