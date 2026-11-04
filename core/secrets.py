"""
Secrets + at-rest encryption helpers.

Two jobs:
  1. Fetch API keys (Gemini, etc) from the OS keychain first, falling back to
     the existing plaintext ``config/api_keys.json``. Nothing is rewritten
     without explicit opt-in; the keychain path is additive and transparent.
  2. Provide a single Fernet key for encrypting ``memory/long_term.json`` at
     rest. The key itself lives in the OS keychain (macOS Keychain, Windows
     Credential Manager, or Linux SecretService) so the on-disk store is no
     longer readable by anything else running under the same user account.

All of this degrades safely when the optional ``keyring`` module is missing
or the OS keychain is unavailable (CI, headless servers, locked keychain on
first boot). In that case we fall back to a project-local key file at
``config/.memory_key`` with 0600 permissions on POSIX. The memory manager
decides whether encryption is enabled by calling ``encryption_available()``.
"""
from __future__ import annotations

import base64
import os
import stat
import sys
from pathlib import Path

# ── Keyring namespace ────────────────────────────────────────────────────────
# A single identifier so entries created by this project are easy to find and
# revoke in the OS keychain UI without touching unrelated apps' secrets.
_SERVICE_NAME = "jarvis-assistant"
_MEMORY_KEY_NAME = "memory_fernet_key_v1"
_API_KEY_PREFIX = "api_key:"

# Project-local fallback for the memory key when no keychain is reachable.
# Not cross-user safe — but the threat model here is "another process on the
# same machine should not be able to cat the memory file", and a 0600 file
# under $HOME/.../config is strictly better than plaintext JSON next to it.
try:
    _BASE_DIR = Path(__file__).resolve().parent.parent
except Exception:
    _BASE_DIR = Path.cwd()
_LOCAL_KEY_PATH = _BASE_DIR / "config" / ".memory_key"


# ── Optional-dependency plumbing ─────────────────────────────────────────────

def _try_keyring():
    """Return the ``keyring`` module if importable AND the backend works.

    Some Linux boxes have the package installed but no DBus / no SecretService
    — ``keyring.get_password`` raises on those. Probe once with a dummy read
    so callers don't have to handle it everywhere.
    """
    try:
        import keyring  # type: ignore
    except Exception:
        return None
    try:
        keyring.get_password(_SERVICE_NAME, "__probe__")
    except Exception:
        return None
    return keyring


def _try_fernet():
    try:
        from cryptography.fernet import Fernet, InvalidToken  # type: ignore
        return Fernet, InvalidToken
    except Exception:
        return None, None


# ── API key lookup ───────────────────────────────────────────────────────────

def get_api_key(name: str = "gemini_api_key") -> str | None:
    """Keychain first, then ``config/api_keys.json``. Returns None if both miss.

    The JSON file is still the source of truth for new deployments — this just
    gives privacy-conscious users a way to move the secret out without us
    forcing the migration. ``set_api_key`` writes to the keychain side only.
    """
    kr = _try_keyring()
    if kr is not None:
        try:
            v = kr.get_password(_SERVICE_NAME, _API_KEY_PREFIX + name)
            if v:
                return v.strip() or None
        except Exception:
            pass
    try:
        from memory.config_manager import load_api_keys
        return (load_api_keys().get(name) or "").strip() or None
    except Exception:
        return None


def set_api_key(name: str, value: str) -> bool:
    """Store an API key in the OS keychain. No-op (returns False) when no
    keychain is reachable — callers can decide whether to warn the user."""
    kr = _try_keyring()
    if kr is None:
        return False
    try:
        kr.set_password(_SERVICE_NAME, _API_KEY_PREFIX + name, value)
        return True
    except Exception:
        return False


# ── Memory-encryption key ────────────────────────────────────────────────────

def encryption_available() -> bool:
    """True only if we can both derive a key AND encrypt with it. False tells
    the memory manager to stay on plaintext and skip the migration branch."""
    Fernet, _ = _try_fernet()
    if Fernet is None:
        return False
    return _memory_key() is not None


def _write_local_key(key: bytes) -> None:
    _LOCAL_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LOCAL_KEY_PATH.write_bytes(key)
    # Best-effort tighten: on POSIX, chmod 600 so other local users can't read
    # the key. Windows ignores the mode bits — rely on the per-user profile.
    try:
        if os.name == "posix":
            os.chmod(_LOCAL_KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def _read_local_key() -> bytes | None:
    try:
        if _LOCAL_KEY_PATH.exists():
            return _LOCAL_KEY_PATH.read_bytes().strip() or None
    except Exception:
        return None
    return None


def _memory_key() -> bytes | None:
    """Return the Fernet key for memory at rest. Created on first call and
    then cached in either the OS keychain or a local 0600 file."""
    Fernet, _ = _try_fernet()
    if Fernet is None:
        return None

    kr = _try_keyring()
    if kr is not None:
        try:
            existing = kr.get_password(_SERVICE_NAME, _MEMORY_KEY_NAME)
            if existing:
                return existing.encode("ascii")
            new_key = Fernet.generate_key()
            kr.set_password(_SERVICE_NAME, _MEMORY_KEY_NAME,
                            new_key.decode("ascii"))
            return new_key
        except Exception:
            pass

    # Keychain unavailable → file fallback. Still better than plaintext memory.
    existing = _read_local_key()
    if existing:
        return existing
    new_key = Fernet.generate_key()
    try:
        _write_local_key(new_key)
    except Exception as e:
        print(f"[Secrets] could not persist local memory key: {e}")
        return None
    return new_key


# ── Encrypt / decrypt ────────────────────────────────────────────────────────

_SENTINEL = b"@fernet:v1\n"


def is_encrypted(blob: bytes) -> bool:
    return isinstance(blob, (bytes, bytearray)) and blob.startswith(_SENTINEL)


def encrypt_bytes(plaintext: bytes) -> bytes | None:
    """Return ciphertext with the ``@fernet:v1\\n`` sentinel prepended, or None
    if encryption isn't possible right now (missing key, missing library)."""
    Fernet, _ = _try_fernet()
    key = _memory_key()
    if Fernet is None or key is None:
        return None
    try:
        token = Fernet(key).encrypt(plaintext)
    except Exception as e:
        print(f"[Secrets] encrypt failed: {e}")
        return None
    return _SENTINEL + token


def decrypt_bytes(blob: bytes) -> bytes | None:
    """Return plaintext if ``blob`` is a ``@fernet:v1`` payload we can open.
    Returns None on any failure (missing key, wrong key, corrupt data) so the
    caller can fall back to the plaintext backup."""
    if not is_encrypted(blob):
        return None
    Fernet, InvalidToken = _try_fernet()
    key = _memory_key()
    if Fernet is None or key is None:
        return None
    token = bytes(blob[len(_SENTINEL):])
    try:
        return Fernet(key).decrypt(token)
    except Exception as e:  # InvalidToken + generic
        print(f"[Secrets] decrypt failed: {type(e).__name__}: {e}")
        return None
