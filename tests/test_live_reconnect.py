"""Tests for bounded Gemini Live connection setup."""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main


def test_stalled_live_setup_times_out_and_closes_connection_context(monkeypatch):
    async def run():
        closed = asyncio.Event()

        @asynccontextmanager
        async def stalled_connect(*, model, config):
            try:
                await asyncio.Event().wait()
                yield None
            finally:
                closed.set()

        client = mock.Mock()
        client.aio.live.connect = stalled_connect
        monkeypatch.setattr(main, "LIVE_CONNECT_TIMEOUT_SECONDS", 0.01)

        try:
            async with main._connect_live_with_timeout(
                client, model="test-model", config=None
            ):
                raise AssertionError("a stalled setup must not yield a session")
        except TimeoutError:
            pass
        else:
            raise AssertionError("stalled Live setup did not time out")

        assert closed.is_set()

    asyncio.run(run())
