"""Tests for keeping an idle Gemini Live connection active."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main


class _Session:
    def __init__(self):
        self.sent = asyncio.Event()
        self.audio = []

    async def send_realtime_input(self, *, audio):
        self.audio.append(audio)
        self.sent.set()


def test_sender_sends_synthetic_silence_when_input_queue_is_idle(monkeypatch):
    async def run():
        monkeypatch.setattr(main, "LIVE_KEEPALIVE_SECONDS", 0.01)
        live = main.JarvisLive.__new__(main.JarvisLive)
        live.out_queue = asyncio.Queue()
        live.session = _Session()

        sender = asyncio.create_task(live._send_realtime())
        try:
            await asyncio.wait_for(live.session.sent.wait(), timeout=1)
        finally:
            sender.cancel()
            try:
                await sender
            except asyncio.CancelledError:
                pass

        assert len(live.session.audio) == 1
        assert live.session.audio[0].data == bytes(main.CHUNK_SIZE * 2)
        assert live.session.audio[0].mime_type == "audio/pcm"

    asyncio.run(run())
