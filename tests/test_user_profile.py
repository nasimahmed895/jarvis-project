"""Tests for memory/user_profile.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from memory import user_profile


@pytest.fixture(autouse=True)
def _tmp_profile(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(user_profile, "_PATH", tmp_path / "user_profile.json")
    yield


def test_set_get_delete_roundtrip():
    user_profile.set("first_name", "Ada")
    assert user_profile.get("first_name") == "Ada"
    user_profile.delete("first_name")
    assert user_profile.get("first_name") is None


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        user_profile.set("not_a_field", "x")


def test_missing_reports_empty_required():
    user_profile.set("first_name", "Ada")
    miss = user_profile.missing(["first_name", "last_name", "email"])
    assert miss == ["last_name", "email"]


def test_match_key_handles_synonyms():
    assert user_profile.match_key("First Name") == "first_name"
    assert user_profile.match_key("e-mail") == "email"
    assert user_profile.match_key("ZIP Code") == "postal_code"
    assert user_profile.match_key("linkedin url") == "linkedin"
    assert user_profile.match_key("please enter your email address *") == "email"


def test_fill_values_maps_by_label_and_reports_missing():
    user_profile.set("first_name", "Ada")
    user_profile.set("email", "ada@example.com")
    detected = [
        {"name": "fname",  "label": "First Name"},
        {"name": "lname",  "label": "Last Name"},
        {"name": "mail",   "label": "Email Address"},
        {"name": "phone",  "label": "Phone"},
        {"name": "misc",   "label": "Describe yourself"},   # not in profile
    ]
    out = user_profile.fill_values(detected)
    assert out["fname"] == "Ada"
    assert out["mail"] == "ada@example.com"
    assert "lname" not in out
    # Missing labels reported for user prompts
    assert "lname" in out["_missing"]
    assert "phone" in out["_missing"]


def test_field_catalogue_shape():
    cat = user_profile.field_catalogue()
    keys = {f["key"] for f in cat}
    assert {"first_name", "last_name", "email", "phone", "address_line1",
            "postal_code", "country"}.issubset(keys)
