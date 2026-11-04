"""
Offline tests for the vector-memory layer.

Gemini is not hit — we monkey-patch `_embed` with a tiny deterministic
embedder (3-dim hash) so retrieval is reproducible without network access.
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from core import vectors


def _fake_embed(text: str) -> list[float]:
    """Deterministic 3-dim embedding: sign+magnitude hash of three slices.
    Not meaningful semantically — just reproducible for similarity checks."""
    if not text:
        return None  # type: ignore
    h = hashlib.sha1(text.lower().encode("utf-8")).hexdigest()
    return [int(h[i:i + 8], 16) / 0xffffffff for i in (0, 8, 16)]


class VectorsTests(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp(prefix="vec-")) / "v.sqlite"
        vectors.connect(self.db)
        # Replace the embedder for deterministic behaviour.
        self._orig_embed = vectors._embed
        self._orig_dim = vectors._EMBED_DIM
        vectors._embed = _fake_embed
        vectors._EMBED_DIM = 3

    def tearDown(self):
        vectors._embed = self._orig_embed
        vectors._EMBED_DIM = self._orig_dim

    def test_add_and_count(self):
        vectors.add("memory", "the user prefers Python")
        vectors.add("memory", "rootdevs is a software company")
        self.assertEqual(vectors.count(), 2)

    def test_vector_search_returns_same_text_first(self):
        text = "the user is working on JARVIS browser agent"
        vectors.add("memory", text)
        vectors.add("memory", "unrelated thought about cats")
        vectors.add("memory", "another unrelated memory")
        hits = vectors.search(text, top_k=3)
        self.assertTrue(hits, "expected results")
        self.assertEqual(hits[0]["text"], text)
        self.assertEqual(hits[0]["kind"], "vector")

    def test_substring_fallback_when_no_embedding(self):
        # Kill the embedder → rows have no vec; search should fall back.
        vectors._embed = lambda s: None  # type: ignore
        vectors.add("memory", "the marmot ate my lunch")
        vectors.add("memory", "nothing useful here")
        hits = vectors.search("marmot")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["kind"], "substring")
        self.assertIn("marmot", hits[0]["text"])

    def test_source_filter(self):
        vectors.add("memory",  "memory entry 1")
        vectors.add("journal", "journal entry 1")
        all_hits = vectors.search("entry", top_k=10)
        self.assertEqual(len(all_hits), 2)
        mem_hits = vectors.search("entry", top_k=10, source="memory")
        self.assertEqual(len(mem_hits), 1)
        self.assertEqual(mem_hits[0]["source"], "memory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
