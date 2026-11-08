"""P3.6 — URL downloader."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from actions import downloader


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str | None = None):
        self._body = body
        self.headers = {"Content-Type": content_type} if content_type else {}
        self._pos = 0

    def read(self, n: int):
        chunk = self._body[self._pos: self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(body: bytes, content_type: str = "application/octet-stream"):
    o = mock.MagicMock()
    o.open.side_effect = lambda req, timeout=30: _FakeResponse(body, content_type)
    return o


class DownloaderTests(unittest.TestCase):
    def test_rejects_non_http_scheme(self):
        with self.assertRaises(ValueError):
            downloader.download("file:///etc/passwd")

    def test_rejects_empty_url(self):
        with self.assertRaises(ValueError):
            downloader.download("")

    def test_downloads_body_to_dst_dir(self):
        d = _tmp("dl-")
        body = b"hello world"
        info = downloader.download(
            "https://example.com/doc.txt",
            dst_dir=d,
            opener=_opener(body, "text/plain"),
        )
        self.assertTrue(info["ok"])
        self.assertEqual(info["bytes"], len(body))
        self.assertEqual(info["content_type"], "text/plain")
        self.assertTrue(Path(info["path"]).exists())
        self.assertEqual(Path(info["path"]).read_bytes(), body)

    def test_size_cap_rejects_oversize(self):
        d = _tmp("dl-")
        big = b"x" * (2 * 1024 * 1024)   # 2 MB
        with self.assertRaises(RuntimeError):
            downloader.download(
                "https://example.com/big.bin",
                dst_dir=d, max_mb=1,
                opener=_opener(big),
            )
        # Partial file was cleaned up.
        self.assertEqual(list(d.iterdir()), [])

    def test_unique_path_on_collision(self):
        d = _tmp("dl-")
        info1 = downloader.download(
            "https://example.com/a.txt", dst_dir=d, opener=_opener(b"1"),
        )
        info2 = downloader.download(
            "https://example.com/a.txt", dst_dir=d, opener=_opener(b"2"),
        )
        self.assertNotEqual(info1["path"], info2["path"])
        self.assertTrue(info2["path"].endswith("_2.txt"))

    def test_safe_filename_sanitizes_path_components(self):
        d = _tmp("dl-")
        info = downloader.download(
            "https://example.com/../weird%20name.pdf",
            dst_dir=d, opener=_opener(b"ok"),
        )
        self.assertNotIn("..", Path(info["path"]).name)
        self.assertIn("weird", Path(info["path"]).name)

    def test_filename_override(self):
        d = _tmp("dl-")
        info = downloader.download(
            "https://example.com/stream",
            filename="my_doc.pdf", dst_dir=d, opener=_opener(b"ok"),
        )
        self.assertEqual(Path(info["path"]).name, "my_doc.pdf")

    def test_tool_entry_missing_url(self):
        self.assertIn("url", downloader._tool_entry({}))

    def test_tool_entry_refused_scheme(self):
        self.assertIn("Refused", downloader._tool_entry({"url": "ftp://x"}))


if __name__ == "__main__":
    unittest.main()
