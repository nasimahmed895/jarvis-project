"""
Lightweight vector-memory layer.

Stores short pieces of text (memory entries, journal digests, notes) along
with their embedding and lets callers do semantic retrieval ("what did I
say about X last week?") without a vector-DB dependency. Primary path uses
the Gemini embedding API — the key is already configured — and a pure-
Python cosine search in-process. If the embedding call fails the record
still lands as text-only; callers get substring fallback.

Schema:
    vectors(id, source, source_id, text, vec BLOB, dim, created_at)

`source` is a short string tag (e.g. "memory", "journal", "task_summary"),
`source_id` links back to the originating row. `vec` is a packed little-
endian float32 array; `dim` records the length so a model change doesn't
mix shapes.
"""
from __future__ import annotations

import array
import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

_LOCK = threading.RLock()
_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None

# Switch to a different embedding model by changing this one constant.
# `text-embedding-004` returns 768-dim vectors. Dimensionality mismatch is
# refused at write time so a late model swap doesn't silently poison the
# index — reindex deliberately, or bump a schema version.
_EMBED_MODEL = "gemini-embedding-001"
_EMBED_DIM   = 3072


def connect(db_path: Path | str) -> None:
    global _DB, _DB_PATH
    with _LOCK:
        if _DB is not None and _DB_PATH == Path(db_path):
            return
        if _DB is not None:
            try:
                _DB.close()
            except Exception:
                pass
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(str(db_path), check_same_thread=False)
        _DB.row_factory = sqlite3.Row
        _DB.execute("PRAGMA journal_mode=WAL")
        _DB_PATH = Path(db_path)
        _init_schema()


def _init_schema() -> None:
    assert _DB is not None
    _DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS vectors (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT    NOT NULL,
            source_id   TEXT    DEFAULT NULL,
            text        TEXT    NOT NULL,
            vec         BLOB    DEFAULT NULL,
            dim         INTEGER DEFAULT NULL,
            created_at  REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vectors_source ON vectors(source);
        """
    )
    _DB.commit()


def _embed(text: str) -> list[float] | None:
    """Call the Gemini embedding API. Returns None on any failure so the
    caller can keep going with text-only storage."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        from google import genai
        from core.gemini import api_key as _api_key
        key = _api_key()
        if not key:
            return None
        cl = genai.Client(api_key=key)
        resp = cl.models.embed_content(model=_EMBED_MODEL, contents=text)
        # SDK shape has shifted a couple of times — accept several.
        embeds = getattr(resp, "embeddings", None)
        if embeds:
            e = embeds[0]
            vals = getattr(e, "values", None) or (e.get("values") if isinstance(e, dict) else None)
            if vals:
                return [float(x) for x in vals]
        embedding = getattr(resp, "embedding", None)
        if embedding is not None:
            vals = getattr(embedding, "values", None) or embedding
            return [float(x) for x in vals]
    except Exception as e:
        print(f"[Vectors] embed failed: {type(e).__name__}: {e}")
    return None


def _pack(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes) -> list[float]:
    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr)


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    num = 0.0
    sa = 0.0
    sb = 0.0
    for x, y in zip(a, b):
        num += x * y
        sa  += x * x
        sb  += y * y
    if sa == 0 or sb == 0:
        return 0.0
    return num / math.sqrt(sa * sb)


# ── Public API ───────────────────────────────────────────────────────────────

def add(source: str, text: str, source_id: str | None = None) -> int:
    """Index a snippet. Returns the row id. If embedding fails the row is
    stored anyway (text-only) so substring search still finds it."""
    assert _DB is not None, "vectors.connect() not called"
    vec = _embed(text)
    blob = _pack(vec) if vec and len(vec) == _EMBED_DIM else None
    dim  = len(vec) if vec else None
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO vectors(source, source_id, text, vec, dim, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (source, source_id, text, blob, dim, time.time()),
        )
        _DB.commit()
        return int(cur.lastrowid)


def search(query: str, top_k: int = 5,
           source: str | Iterable[str] | None = None) -> list[dict]:
    """Return the top_k rows most similar to `query`. When the embedding
    call fails (or the row has no embedding), falls back to substring match.
    Each hit: {id, source, source_id, text, score, kind}."""
    assert _DB is not None, "vectors.connect() not called"
    query = (query or "").strip()
    if not query:
        return []
    qvec = _embed(query)
    with _LOCK:
        if source is None:
            rows = _DB.execute(
                "SELECT id, source, source_id, text, vec FROM vectors"
            ).fetchall()
        else:
            if isinstance(source, str):
                source = [source]
            ph = ",".join("?" * len(list(source)))
            rows = _DB.execute(
                f"SELECT id, source, source_id, text, vec FROM vectors "
                f"WHERE source IN ({ph})", list(source),
            ).fetchall()
    scored: list[tuple[float, dict, str]] = []
    q_low = query.lower()
    for r in rows:
        d = dict(r)
        vec_blob = d.pop("vec")
        if qvec and vec_blob is not None:
            v = _unpack(vec_blob)
            if len(v) == len(qvec):
                scored.append((_cosine(qvec, v), d, "vector"))
                continue
        # Substring fallback.
        text_low = (d.get("text") or "").lower()
        if q_low in text_low:
            # Cheap relevance: fraction of query matched, biased down from vectors.
            score = 0.4 + 0.3 * (len(q_low) / max(1, len(text_low)))
            scored.append((score, d, "substring"))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[dict] = []
    for score, d, kind in scored[:top_k]:
        out.append({**d, "score": round(float(score), 4), "kind": kind})
    return out


def count() -> int:
    with _LOCK:
        return int(_DB.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
