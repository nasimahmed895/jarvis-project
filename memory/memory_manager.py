import json
import re
from datetime import datetime
from threading import Lock
from pathlib import Path
import sys


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = BASE_DIR / "memory" / "long_term.json"
# Written next to long_term.json during the first encrypted save. Kept for a
# conservative TTL so a keychain lockout doesn't cost the user their memory —
# worst case they can copy it back to long_term.json by hand.
BACKUP_PATH      = BASE_DIR / "memory" / "long_term.plaintext.bak"
_lock            = Lock()
MAX_VALUE_LENGTH = 380

# ── Why there are two very different numbers here ────────────────────────────
#
# There used to be one: MEMORY_MAX_CHARS = 2200, applied to the whole store. It
# was a *storage* limit, and it existed only because the entire memory was
# pasted into the system prompt on every connect — so growing the memory grew
# every single request. When it filled, _trim_to_limit() deleted the oldest
# entries and printed one line to a console nobody reads. A memory described as
# "deeply remembers projects, preferences and personal context" was in practice
# two pages long, and quietly forgot your sister's name after a few weeks.
#
# Storage and prompt budget are now separate concerns:
#
#   MEMORY_MAX_CHARS  — a runaway guard, not a feature limit. Nothing normal
#                       reaches it; a bug writing in a loop does.
#   PROMPT_CORE_CHARS — what actually rides in the system prompt every session.
#                       Smaller than the old whole-memory dump, so sessions
#                       start *faster* than before, not slower.
#
# Everything above the core stays on disk and is fetched on demand by the
# recall_memory tool — see search_memory() and format_memory_for_prompt().
MEMORY_MAX_CHARS  = 200_000
PROMPT_CORE_CHARS = 900
PROMPT_INDEX_CHARS = 420
# Budget for the lessons block injected into every prompt. Closes the reflection
# feedback loop: the daily reflection pass wrote `learned/<date>` entries but
# nothing ever read them back — lessons were orphaned. 500 chars holds roughly
# the 6-10 most recent bullets without dominating the system prompt.
PROMPT_LESSONS_CHARS = 500
PROMPT_LESSONS_MAX   = 10
# Most entries any one category may contribute to the core block, so a person
# with forty stored preferences still gets their sister into the prompt.
PROMPT_MAX_PER_CATEGORY = 6

def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
        "learned":       {},
    }

def _decode_memory_bytes(blob: bytes) -> dict | None:
    """Try encrypted-format first, then plaintext JSON. Returns None on both
    failing so the caller can fall back to the plaintext backup."""
    try:
        from core import secrets as _secrets  # lazy: avoids cycles at import time
        if _secrets.is_encrypted(blob):
            plain = _secrets.decrypt_bytes(blob)
            if plain is None:
                return None
            data = json.loads(plain.decode("utf-8"))
            return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"[Memory] ⚠️ encrypted decode failed: {e}")
        return None
    try:
        data = json.loads(blob.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return _empty_memory()
    with _lock:
        try:
            blob = MEMORY_PATH.read_bytes()
            data = _decode_memory_bytes(blob)
            if data is None and BACKUP_PATH.exists():
                # Encrypted store is unreadable (keychain lockout, moved
                # machine, wrong user). The plaintext backup kept from the
                # migration moment is a lifeline — read it and let the next
                # save re-encrypt under the current key.
                try:
                    data = json.loads(
                        BACKUP_PATH.read_text(encoding="utf-8")
                    )
                    print("[Memory] ⚠️ encrypted load failed — restored from plaintext backup")
                except Exception:
                    data = None
            if isinstance(data, dict):
                base = _empty_memory()
                for key in base:
                    if key not in data:
                        data[key] = {}
                return data
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] ⚠️ Load error: {e}")
            return _empty_memory()

def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


# Set by main.py so a trim can reach the activity log. Deleting something a
# person told you and mentioning it only on stdout is how a memory loses trust.
_trim_notifier = None


def set_trim_notifier(fn) -> None:
    """Register a callable(str) that surfaces trims to the user."""
    global _trim_notifier
    _trim_notifier = fn


# Importance weights for trim scoring. Higher = harder to forget. Identity is
# given an outsized weight because losing someone's name is a trust-breaker —
# the trim loop also refuses to drop identity entries unless nothing else
# remains, which rarely happens before the 200 KB runaway guard trips. Learned
# lessons sit above notes because the reflection pass already filtered them.
_CATEGORY_WEIGHT = {
    "identity":      5.0,
    "relationships": 3.0,
    "projects":      3.0,
    "preferences":   2.0,
    "wishes":        2.0,
    "learned":       2.0,
    "notes":         1.0,
    "sessions":      0.5,
}

# Recency half-life in days: a 30-day-old entry is worth half a same-day one,
# a 60-day-old a quarter, and so on. This is why a fresh "notes" entry can
# outrank a stale "preferences" one — both freshness and category matter.
_RECENCY_HALFLIFE_DAYS = 30.0


def _days_ago(updated: str) -> float:
    if not updated:
        return 999.0
    try:
        d = datetime.strptime(updated, "%Y-%m-%d")
    except Exception:
        return 999.0
    delta = datetime.now() - d
    return max(0.0, delta.days + delta.seconds / 86400.0)


def _trim_score(cat: str, entry: dict) -> float:
    """Higher score = more important = less likely to be dropped.

    Pure chronological trim was flagged by the audit: it would drop a
    permanent fact like 'sister_name' before a transient 'notes' entry that
    happened to be newer. Scoring blends category weight with recency so the
    categories that actually matter in conversation are protected."""
    updated = entry.get("updated", "") if isinstance(entry, dict) else ""
    days = _days_ago(updated)
    recency = 1.0 / (1.0 + days / _RECENCY_HALFLIFE_DAYS)
    weight = _CATEGORY_WEIGHT.get(cat, 1.0)
    return weight * recency


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    entries = _all_entries(memory)
    # Two-pass drop: non-identity entries first (ascending by score, so the
    # cheapest ones leave), identity only if the runaway guard is still tripped
    # afterwards. A memory that has filled with 200 KB of bugged writes should
    # not drop the user's name before the garbage it is being drowned in.
    non_identity = [t for t in entries if t[0] != "identity"]
    non_identity.sort(key=lambda t: _trim_score(t[0], t[2]))
    identity = [t for t in entries if t[0] == "identity"]
    identity.sort(key=lambda t: _trim_score(t[0], t[2]))
    ordered = non_identity + identity

    dropped: list[str] = []
    for cat, key, _ in ordered:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        dropped.append(f"{cat}/{key}")
        print(f"[Memory] 🗑️  Trimmed {cat}/{key}")
    if dropped and _trim_notifier:
        try:
            _trim_notifier(
                f"SYS: Memory full — forgot {len(dropped)} lowest-value entries "
                f"({', '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''})"
            )
        except Exception:
            pass
    return memory

def save_memory(memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory = _trim_to_limit(memory)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    plaintext = json.dumps(memory, indent=2, ensure_ascii=False).encode("utf-8")
    ciphertext: bytes | None = None
    try:
        from core import secrets as _secrets
        if _secrets.encryption_available():
            ciphertext = _secrets.encrypt_bytes(plaintext)
    except Exception as e:
        print(f"[Memory] ⚠️ encryption check failed: {e}")
        ciphertext = None
    with _lock:
        if ciphertext is not None:
            # First time we write encrypted output: preserve a plaintext snapshot
            # so a keychain lockout is recoverable. Subsequent saves overwrite
            # just the ciphertext file — we are NOT fanning out plaintext on
            # every save.
            if not BACKUP_PATH.exists() and MEMORY_PATH.exists():
                try:
                    existing = MEMORY_PATH.read_bytes()
                    # Only snapshot genuinely plaintext input, never an older
                    # ciphertext (would double-encrypt the recovery path).
                    try:
                        from core import secrets as _s2
                        is_enc = _s2.is_encrypted(existing)
                    except Exception:
                        is_enc = existing.startswith(b"@fernet:")
                    if not is_enc:
                        BACKUP_PATH.write_bytes(existing)
                except Exception as e:
                    print(f"[Memory] ⚠️ backup snapshot failed: {e}")
            MEMORY_PATH.write_bytes(ciphertext)
        else:
            MEMORY_PATH.write_bytes(plaintext)


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _normalize_for_dedup(val: str) -> str:
    """Case-fold + collapse whitespace so trivial variants of the same answer
    collide. Punctuation is kept because "blue, please" and "blue" are
    legitimately different."""
    return " ".join(str(val).strip().casefold().split())


def _sibling_has_value(siblings: dict, normalized: str,
                       excluded_key: str | None = None) -> str | None:
    """Return the name of a sibling leaf already holding the same normalized
    value, or None. Used by _recursive_update to drop writes that would create
    a duplicate record under a different key within the same category."""
    for k, v in siblings.items():
        if k == excluded_key:
            continue
        if not isinstance(v, dict):
            continue
        existing = v.get("value")
        if existing is None:
            continue
        if _normalize_for_dedup(existing) == normalized:
            return k
    return None


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val  = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            existing = target.get(key, {})
            if isinstance(existing, dict) and existing.get("value") == new_val:
                continue   # exact same key+value already present; no-op.
            # Duplicate-across-keys check: if another sibling leaf already
            # stores the same normalized value, drop the write so the memory
            # does not accumulate parallel aliases ("favorite_color":"blue"
            # and "color":"blue"). The original key wins — timestamps are
            # not refreshed to avoid a write amplification loop.
            normalized = _normalize_for_dedup(new_val)
            dup = _sibling_has_value(target, normalized, excluded_key=key)
            if dup is not None:
                print(f"[Memory] ↷ duplicate skipped: {key}={new_val!r} already under {dup!r}")
                continue
            target[key] = {
                "value":   new_val,
                "updated": datetime.now().strftime("%Y-%m-%d"),
            }
            changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    memory = load_memory()
    if _recursive_update(memory, memory_update):
        save_memory(memory)
        print(f"[Memory] 💾 Saved: {list(memory_update.keys())}")
    return memory

def _entry_value(entry) -> str:
    """Accept both the {'value': ..., 'updated': ...} shape and a bare string,
    because early versions of the store wrote plain strings."""
    if isinstance(entry, dict):
        return str(entry.get("value", "") or "").strip()
    return str(entry or "").strip()


def _pretty(key: str) -> str:
    return key.replace("_", " ").strip()


# Identity is always in the prompt; these categories compete for the remaining
# budget by recency.
_CATEGORY_LABELS = {
    "preferences":   "Preferences",
    "projects":      "Active projects / goals",
    "relationships": "People in their life",
    "wishes":        "Wishes / plans",
    "notes":         "Notes",
}

_IDENTITY_FIELDS = ["name", "age", "birthday", "city", "job",
                    "language", "school", "nationality"]


def _collect_lessons(memory: dict | None) -> list[str]:
    """Flatten memory['learned'] into a newest-first list of lesson bullets.

    Each daily entry stores multiple lessons joined with '•' bullets (see
    core/reflection._write_lessons). We split them back out and keep insertion
    order within a day, with newer days first.
    """
    if not memory:
        return []
    learned = memory.get("learned") or {}
    if not isinstance(learned, dict) or not learned:
        return []
    # Sort days newest first. Days are ISO dates, so lexicographic == chronological.
    days = sorted(learned.keys(), reverse=True)
    out: list[str] = []
    for day in days:
        entry = learned[day]
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value", "") or "").strip()
        if not value:
            continue
        for line in value.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("•"):
                line = line[1:].strip()
            if line:
                out.append(line)
        if len(out) >= PROMPT_LESSONS_MAX * 2:
            break
    return out


def format_memory_for_prompt(memory: dict | None) -> str:
    """Build the memory block that goes into the system prompt.

    This used to dump everything. It now sends three things:

      1. IDENTITY  - always, in full. It is small, and it is wrong for the
         assistant to have to look up your name.
      2. RECENT    - the most recently updated entries from every other
         category, up to PROMPT_CORE_CHARS. Recency is the cheapest useful
         relevance signal available without embeddings.
      3. AN INDEX  - the *keys* of everything else, values omitted.

    Point 3 is what makes recall work at all. A model cannot decide to look
    something up if it does not know the thing exists: with only points 1 and 2,
    "who is Ayse?" would get "I don't know" while ayse_sister sat on disk
    unread. The index costs a few hundred characters and turns recall from a
    gamble into a lookup.

    Net effect on latency: this block is SMALLER than the old full dump, so
    every session connects with fewer tokens. Occasionally the model spends one
    extra round trip on recall_memory - covered by the acknowledgment it
    already speaks before any slow step."""
    if not memory:
        return ""

    core_lines: list[str] = []

    # 1. Identity - always, in full
    identity = memory.get("identity", {}) or {}
    for field in _IDENTITY_FIELDS:
        val = _entry_value(identity.get(field))
        if not val:
            continue
        if field == "language":
            # Labelled as an observation, not a setting. A bare "Language:
            # English" line written months ago reads like a standing order and
            # was one of the reasons a Turkish question came back in English.
            core_lines.append(
                f"Has spoken to you in: {val} (an observation about the past — "
                f"always answer in the language of their CURRENT message)")
        else:
            core_lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in _IDENTITY_FIELDS:
            continue
        val = _entry_value(entry)
        if val:
            core_lines.append(f"{_pretty(key).title()}: {val}")

    # 2. Everything else, most recently updated first
    rest: list[tuple[str, str, str, str]] = []   # (updated, cat, key, value)
    for cat in _CATEGORY_LABELS:
        for key, entry in (memory.get(cat, {}) or {}).items():
            val = _entry_value(entry)
            if not val:
                continue
            updated = (entry.get("updated", "") if isinstance(entry, dict) else "") or "0000-00-00"
            rest.append((updated, cat, key, val))
    rest.sort(key=lambda t: t[0], reverse=True)

    used    = sum(len(l) + 1 for l in core_lines)
    shown: dict[str, list[str]] = {}
    overflow: dict[str, list[str]] = {}

    # Recency decides order, but no single category may take the whole budget.
    # Without the cap, someone with forty stored preferences gets a prompt that
    # is forty preferences and not one person's name — the categories that
    # matter most in conversation are also the ones that change least often, so
    # pure recency systematically buries them.
    per_cat_used: dict[str, int] = {}
    for _updated, cat, key, val in rest:
        line = f"  - {_pretty(key).title()}: {val}"
        if (per_cat_used.get(cat, 0) < PROMPT_MAX_PER_CATEGORY
                and used + len(line) + 1 <= PROMPT_CORE_CHARS):
            shown.setdefault(cat, []).append(line)
            per_cat_used[cat] = per_cat_used.get(cat, 0) + 1
            used += len(line) + 1
        else:
            overflow.setdefault(cat, []).append(_pretty(key))

    # The index is a table of contents, so it is interleaved across categories
    # rather than continuing in recency order. Sorted by recency it would list
    # twenty-four preferences before the first relationship, and the one entry
    # the index exists for — the old fact the model has no other way to know
    # about — would fall off the end.
    indexed: list[str] = []
    if overflow:
        cats  = [c for c in _CATEGORY_LABELS if overflow.get(c)]
        cursor = {c: 0 for c in cats}
        while cats:
            for cat in list(cats):
                i = cursor[cat]
                if i >= len(overflow[cat]):
                    cats.remove(cat)
                    continue
                indexed.append(overflow[cat][i])
                cursor[cat] = i + 1

    for cat, label in _CATEGORY_LABELS.items():
        if shown.get(cat):
            core_lines.append("")
            core_lines.append(f"{label}:")
            core_lines.extend(shown[cat])

    if not core_lines and not indexed:
        return ""

    out = [
        "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]",
        *core_lines,
    ]

    # 3. The index of what is on disk but not in this prompt
    if indexed:
        budget, names = PROMPT_INDEX_CHARS, []
        for n in indexed:
            if budget - len(n) - 2 < 0:
                break
            names.append(n)
            budget -= len(n) + 2
        if names:
            out.append("")
            out.append(
                "[ALSO REMEMBERED — values not shown here. Call recall_memory "
                "with a keyword to read any of these before saying you do not know]"
            )
            out.append(", ".join(names)
                       + (f" (+{len(indexed) - len(names)} more)"
                          if len(indexed) > len(names) else ""))

    # 4. Lessons distilled by the daily reflection pass — things that went
    # wrong, things that worked, user corrections. Previously these were
    # written to disk but never read; this block closes the feedback loop.
    lessons = _collect_lessons(memory)
    if lessons:
        budget   = PROMPT_LESSONS_CHARS
        kept: list[str] = []
        for ls in lessons:
            if len(kept) >= PROMPT_LESSONS_MAX:
                break
            line = f"• {ls}"
            if budget - len(line) - 1 < 0:
                break
            kept.append(line)
            budget -= len(line) + 1
        if kept:
            out.append("")
            out.append(
                "[LESSONS FROM PAST SESSIONS — apply these quietly; do not "
                "recite them. Newest first]"
            )
            out.extend(kept)

    return "\n".join(out) + "\n"


# ── Recall ────────────────────────────────────────────────────────────────────

def _score(query_words: list[str], cat: str, key: str, value: str) -> int:
    """Cheap lexical relevance. No embeddings, no network, no model call - this
    runs in well under a millisecond, which is the entire point: recall must
    cost one model round trip, never two."""
    hay_key = _pretty(key).lower()
    hay_val = value.lower()
    score   = 0
    for w in query_words:
        if not w:
            continue
        if w == hay_key:
            score += 10
        elif w in hay_key:
            score += 6
        if w in hay_val:
            score += 3
        if w in cat:
            score += 1
    return score


def search_memory(query: str, limit: int = 8) -> str:
    """Find stored facts matching `query`. Backs the recall_memory tool.

    An empty query is treated as "show me everything you know", capped - the
    model asks that when the user says "what do you remember about me?"."""
    memory = load_memory()
    words  = [w for w in re.split(r"[^\w]+", (query or "").lower()) if len(w) > 1]

    rows: list[tuple[int, str, str, str]] = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue                     # skip 'sessions', which is a list
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            s = _score(words, cat, key, val) if words else 1
            if s > 0:
                rows.append((s, cat, key, val))

    if not rows:
        return (f"Nothing stored about '{query}'." if query
                else "I have not stored anything about this person yet.")

    rows.sort(key=lambda r: (-r[0], r[2]))
    lines = [f"{cat}/{_pretty(key)}: {val}" for _s, cat, key, val in rows[:max(1, limit)]]
    head  = (f"Stored facts matching '{query}':" if query
             else "Everything currently stored:")
    more  = (f"\n(+{len(rows) - len(lines)} more — search with a narrower keyword)"
             if len(rows) > len(lines) else "")
    return head + "\n" + "\n".join(lines) + more


def all_entries_for_ui() -> list[dict]:
    """Flat list for the memory panel: what JARVIS knows, and when it learned it.
    Sorted newest first so the panel opens on what changed most recently."""
    memory = load_memory()
    rows = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            rows.append({
                "category": cat,
                "key":      key,
                "value":    val,
                "updated":  (entry.get("updated", "") if isinstance(entry, dict) else ""),
            })
    rows.sort(key=lambda r: (r["updated"] or "0000-00-00"), reverse=True)
    return rows

def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    if category not in valid:
        category = "notes"
    update_memory({category: {key: {"value": value}}})
    return f"Remembered: {category}/{key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    memory = load_memory()
    cat    = memory.get(category, {})
    if key in cat:
        del cat[key]
        memory[category] = cat
        save_memory(memory)
        return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


forget_memory = forget


# ── Session memory ─────────────────────────────────────────────────────────────

_SESSION_MAX = 3   # safety cap — in practice 0-1 entries after pop


def save_session_summary(summary: str, language: str = "") -> None:
    """Append a 1-2 sentence session summary to long_term.json['sessions']."""
    summary = (summary or "").strip()
    if not summary:
        return
    memory   = load_memory()
    sessions = memory.get("sessions", [])
    if not isinstance(sessions, list):
        sessions = []
    entry: dict = {
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "summary": summary[:280],
    }
    if language:
        entry["language"] = language
    sessions.append(entry)
    memory["sessions"] = sessions[-_SESSION_MAX:]
    # Reuse save_memory so this path gets encryption, trimming, and the
    # backup snapshot for free. Previously this wrote raw JSON straight to
    # disk and would have shipped plaintext even on encrypted deployments.
    save_memory(memory)
    print(f"[Memory] 📝 Session saved ({entry['date']}): {summary[:60]}…")


def pop_last_session() -> dict | None:
    """
    Return AND remove the most recent session entry.
    Calling this consumes the entry so it is never repeated in future briefings.
    """
    if not MEMORY_PATH.exists():
        return None
    try:
        memory   = load_memory()
        sessions = memory.get("sessions", [])
        if not isinstance(sessions, list) or not sessions:
            return None
        entry = sessions.pop()          # remove the last entry
        memory["sessions"] = sessions
        save_memory(memory)
        return entry
    except Exception as e:
        print(f"[Memory] ⚠️ pop_last_session error: {e}")
        return None