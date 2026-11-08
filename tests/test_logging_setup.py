"""Unit tests for core.logging_setup — structured JSON logger + trace IDs."""

import io
import json
import logging

import pytest

from core import logging_setup


@pytest.fixture(autouse=True)
def _reset_logger_state(monkeypatch):
    """Each test gets a clean logging root + reset init flag."""
    monkeypatch.setattr(logging_setup, "_initialized", False)
    root = logging.getLogger()
    prev_handlers = list(root.handlers)
    prev_level    = root.level
    root.handlers.clear()
    yield
    root.handlers.clear()
    for h in prev_handlers:
        root.addHandler(h)
    root.setLevel(prev_level)


def _capture_stderr(tmp_path):
    """Install logging with a StringIO stderr handler and a file in tmp_path."""
    logging_setup.init_logging(log_path=tmp_path / "jarvis.log")
    root = logging.getLogger()
    buf = io.StringIO()
    stream_handler = logging.StreamHandler(buf)
    stream_handler.setFormatter(logging_setup._JsonFormatter())
    root.addHandler(stream_handler)
    return buf


def test_init_logging_is_idempotent(tmp_path):
    logging_setup.init_logging(log_path=tmp_path / "jarvis.log")
    handler_count = len(logging.getLogger().handlers)
    logging_setup.init_logging(log_path=tmp_path / "jarvis.log")
    assert len(logging.getLogger().handlers) == handler_count


def test_json_formatter_emits_structured_record(tmp_path):
    buf = _capture_stderr(tmp_path)
    log = logging_setup.get_logger("jarvis.test")
    log.info("hello", extra={"tool": "ping"})
    line = buf.getvalue().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["level"] == "INFO"
    assert rec["logger"] == "jarvis.test"
    assert rec["msg"] == "hello"
    assert rec["tool"] == "ping"
    assert "ts" in rec


def test_trace_id_appears_in_record(tmp_path):
    buf = _capture_stderr(tmp_path)
    log = logging_setup.get_logger("jarvis.trace")
    trace = logging_setup.new_trace_id()
    with logging_setup.with_trace_id(trace):
        log.info("inside")
    log.info("outside")
    lines = [json.loads(l) for l in buf.getvalue().strip().splitlines()]
    inside  = next(r for r in lines if r["msg"] == "inside")
    outside = next(r for r in lines if r["msg"] == "outside")
    assert inside["trace_id"] == trace
    assert outside["trace_id"] is None


def test_new_trace_id_is_unique_and_hex():
    seen = {logging_setup.new_trace_id() for _ in range(100)}
    assert len(seen) == 100
    for t in seen:
        int(t, 16)   # not hex → ValueError


def test_exception_formatting_captured(tmp_path):
    buf = _capture_stderr(tmp_path)
    log = logging_setup.get_logger("jarvis.err")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("fail")
    rec = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert rec["level"] == "ERROR"
    assert "RuntimeError: boom" in rec["exc"]


def test_file_handler_writes_to_log_path(tmp_path):
    path = tmp_path / "jarvis.log"
    logging_setup.init_logging(log_path=path)
    log = logging_setup.get_logger("jarvis.file")
    log.warning("written", extra={"k": 1})
    for h in logging.getLogger().handlers:
        h.flush()
    text = path.read_text(encoding="utf-8").strip()
    assert text
    rec = json.loads(text.splitlines()[-1])
    assert rec["msg"] == "written"
    assert rec["k"] == 1
