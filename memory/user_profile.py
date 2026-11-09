"""
memory/user_profile.py — structured identity for form-filling.

The existing `browser_control.set_pref / get_prefs / clear_prefs` surface
stores free-form key/value pairs. That is fine for ad-hoc prefs but a
generic form-filling flow needs a *typed* profile with a known field set
so the mapper can answer "do I have information for this field?" without
guessing at the key.

This module is a thin typed wrapper over a single JSON file at
`memory/user_profile.json`. It does NOT duplicate storage — it offers a
canonical list of fields, a set/get/delete API, a `missing()` helper
that lists required-but-empty fields for a given form shape, and a
`fill_values()` helper that maps a form's detected fields to profile
values.

Sensitive fields (passwords, payment, government IDs) are explicitly
excluded from this store. Credentials live in the OS keyring via
`core/secrets.py`; payment data lives in the user's browser and is never
staged here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


_PATH = Path(__file__).resolve().parent / "user_profile.json"


# Canonical fields with their category + synonyms. Synonyms are matched
# case-insensitively against detected form field labels / names / placeholders.
@dataclass(frozen=True)
class Field:
    key: str
    category: str
    label: str
    synonyms: tuple[str, ...]
    sensitive: bool = False


FIELDS: tuple[Field, ...] = (
    # Identity
    Field("first_name",       "identity", "First name",
          ("first name", "given name", "fname", "firstname",
           "forename", "prenom")),
    Field("last_name",        "identity", "Last name",
          ("last name", "surname", "family name", "lname",
           "lastname", "apellido")),
    Field("preferred_name",   "identity", "Preferred name",
          ("preferred name", "nickname", "display name")),
    Field("date_of_birth",    "identity", "Date of birth (YYYY-MM-DD)",
          ("date of birth", "dob", "birthday", "birthdate")),
    Field("gender",           "identity", "Gender (optional)",
          ("gender", "sex")),

    # Contact
    Field("email",            "contact", "Email address",
          ("email", "e-mail", "email address", "mail")),
    Field("phone",             "contact", "Phone number",
          ("phone", "mobile", "cell", "telephone", "phone number")),

    # Address
    Field("address_line1",     "address", "Address line 1",
          ("address", "street", "street address", "address 1",
           "address line 1")),
    Field("address_line2",     "address", "Address line 2",
          ("address 2", "address line 2", "apt", "suite", "unit")),
    Field("city",              "address", "City",
          ("city", "town", "locality")),
    Field("state",             "address", "State / province",
          ("state", "province", "region")),
    Field("postal_code",       "address", "Postal / zip code",
          ("zip", "zip code", "postal code", "postcode")),
    Field("country",           "address", "Country",
          ("country", "nation")),

    # Professional
    Field("company",           "professional", "Company / employer",
          ("company", "employer", "organisation", "organization")),
    Field("job_title",         "professional", "Job title",
          ("job title", "title", "role", "position")),
    Field("website",           "professional", "Website / portfolio",
          ("website", "portfolio", "url", "homepage")),
    Field("linkedin",          "professional", "LinkedIn URL",
          ("linkedin", "linkedin url")),
    Field("github",            "professional", "GitHub URL",
          ("github", "github url")),

    # Education
    Field("highest_degree",    "education", "Highest degree",
          ("degree", "highest degree", "qualification")),
    Field("institution",       "education", "Institution / school",
          ("school", "university", "institution", "college")),

    # Document locations (paths only — not the contents)
    Field("resume_path",       "documents", "Resume file path",
          ("resume", "cv", "resume file", "cv file")),
    Field("cover_letter_path", "documents", "Cover letter path",
          ("cover letter", "cover letter file")),
)

# For reverse lookup.
_BY_KEY = {f.key: f for f in FIELDS}
_SYNONYM_INDEX: dict[str, str] = {}
for _f in FIELDS:
    for _s in _f.synonyms:
        _SYNONYM_INDEX[_s.lower()] = _f.key
    _SYNONYM_INDEX[_f.key] = _f.key  # key maps to itself


def _load() -> dict:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2, sort_keys=True),
                     encoding="utf-8")


# ── Public API ─────────────────────────────────────────────────────────

def get(key: str) -> Optional[str]:
    key = (key or "").lower().strip()
    if key not in _BY_KEY:
        return None
    return _load().get(key)


def set(key: str, value: str) -> None:
    key = (key or "").lower().strip()
    if key not in _BY_KEY:
        raise KeyError(f"unknown profile field: {key}")
    if _BY_KEY[key].sensitive:
        raise ValueError(f"'{key}' is marked sensitive — not stored here")
    data = _load()
    if value in ("", None):
        data.pop(key, None)
    else:
        data[key] = str(value)
    _save(data)


def delete(key: str) -> None:
    key = (key or "").lower().strip()
    data = _load()
    if key in data:
        data.pop(key, None)
        _save(data)


def all() -> dict:
    return _load()


def missing(required: Iterable[str]) -> list[str]:
    """Return the subset of `required` keys that are empty in the profile."""
    data = _load()
    out: list[str] = []
    for k in required:
        key = (k or "").lower().strip()
        if key not in _BY_KEY:
            continue
        if not data.get(key):
            out.append(key)
    return out


def match_key(label: str) -> Optional[str]:
    """Return the canonical profile key matching a form label, or None."""
    if not label:
        return None
    l = label.lower().strip().strip(":")
    if l in _SYNONYM_INDEX:
        return _SYNONYM_INDEX[l]
    # Contains-match for noisy labels ("Please enter your email address *")
    for synonym, key in _SYNONYM_INDEX.items():
        if synonym in l:
            return key
    return None


def fill_values(fields: Iterable[dict]) -> dict:
    """Given a list of detected form fields (each a dict with at least one
    of `label` / `name` / `placeholder`), return a `{form_field_name:
    profile_value}` dict for the ones we can fill, plus a `_missing`
    sub-dict with the label text for keys the profile does not have a
    value for.

    The browser_control.fill_form tool already accepts an `{fieldname:
    value}` dict, so the output of this helper is ready to pass through.
    """
    data = _load()
    out: dict[str, str] = {}
    missing_: dict[str, str] = {}
    for field in fields:
        text = (field.get("label") or field.get("name") or
                field.get("placeholder") or "")
        key = match_key(text)
        if not key:
            continue
        value = data.get(key)
        slot = field.get("name") or field.get("id") or text
        if value:
            out[slot] = value
        else:
            missing_[slot] = _BY_KEY[key].label
    if missing_:
        out["_missing"] = missing_  # consumer inspects and asks user
    return out


def field_catalogue() -> list[dict]:
    """Return the canonical field list for the dashboard editor."""
    return [{"key": f.key, "category": f.category,
             "label": f.label, "synonyms": list(f.synonyms)}
            for f in FIELDS]
