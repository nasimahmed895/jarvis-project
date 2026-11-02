"""Regression tests for the report-location resolver.

Preference order:
    1. ~/Downloads/JARVIS Reports
    2. ~/Documents/JARVIS Reports
    3. configured directory
    4. tempfile fallback
"""
from __future__ import annotations

from pathlib import Path

import pytest

from market.reports import location as loc


def test_prefers_downloads_when_writable(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    (fake_home / "Downloads").mkdir(parents=True)
    (fake_home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    out = loc.resolve_report_dir(configured=tmp_path / "project_reports")
    assert out.source == "downloads"
    assert "Downloads" in str(out.path)


def test_falls_back_to_documents(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    (fake_home / "Documents").mkdir(parents=True)
    # Deliberately NO Downloads folder.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    out = loc.resolve_report_dir(configured=tmp_path / "project_reports")
    assert out.source == "documents"
    assert "Documents" in str(out.path)


def test_falls_back_to_configured(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    # No Downloads, no Documents. Configured dir is writable.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    configured = tmp_path / "project_reports"
    out = loc.resolve_report_dir(configured=configured)
    assert out.source == "configured"
    assert out.path == configured.resolve()


def test_falls_back_to_tempfile(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # Force every candidate to be rejected by the writable probe. The
    # resolver's tempfile branch is the only path left.
    monkeypatch.setattr(loc, "_probe_writable", lambda d: False)
    out = loc.resolve_report_dir(configured=tmp_path / "project_reports")
    assert out.source == "tempfile"
    assert out.path.exists()


def test_resolved_path_resolves_and_exists(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    (fake_home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    out = loc.resolve_report_dir(configured=None)
    assert out.path.exists()
    assert out.path.is_dir()


def test_note_mentions_actual_location(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    (fake_home / "Downloads").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    out = loc.resolve_report_dir(configured=tmp_path / "project_reports")
    assert str(out.path) in out.note or "Downloads" in out.note
