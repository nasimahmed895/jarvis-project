"""
actions/file_dedupe.py — find duplicate files in a directory tree (P2.4).

Two-stage scan:
  1. Group candidates by file size (cheap, no I/O beyond stat).
  2. Only hash files sharing a size bucket — sha256 of file content.

This keeps a dedupe of a large tree cheap on cold caches: unique sizes are
never hashed, zero-byte files are filtered out, symlinks are skipped so we
do not double-count a mirrored file.

The tool is read-only. Any destructive action (deleting duplicates) stays
with the user / `file_controller` + `confirm` — this module only reports.
"""
from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

_HASH_BUFFER = 1024 * 1024   # 1 MiB chunks keep memory flat on huge files


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_BUFFER)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def scan(root: str | Path,
         recursive: bool = True,
         min_bytes: int = 1,
         follow_symlinks: bool = False) -> list[dict]:
    """Return a list of duplicate groups.

    Each group:
        {"hash": "<sha256>", "size": int, "paths": [str, ...]}

    Groups with only one member (unique content) are omitted. Sorted by
    `size * (len(paths) - 1)` descending so the biggest wins land first.
    """
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        raise FileNotFoundError(root_path)

    size_buckets: dict[int, list[Path]] = defaultdict(list)
    for p in _walk(root_path, recursive=recursive,
                   follow_symlinks=follow_symlinks):
        try:
            if not p.is_file():
                continue
            size = p.stat().st_size
        except OSError:
            continue
        if size < min_bytes:
            continue
        size_buckets[size].append(p)

    hash_groups: dict[tuple[str, int], list[Path]] = defaultdict(list)
    for size, candidates in size_buckets.items():
        if len(candidates) < 2:
            continue   # unique size = unique content, no hash needed
        for p in candidates:
            try:
                digest = _sha256(p)
            except OSError:
                continue
            hash_groups[(digest, size)].append(p)

    out: list[dict] = []
    for (digest, size), paths in hash_groups.items():
        if len(paths) < 2:
            continue
        out.append({
            "hash":  digest,
            "size":  size,
            "paths": sorted(str(p) for p in paths),
        })
    out.sort(key=lambda g: g["size"] * (len(g["paths"]) - 1), reverse=True)
    return out


def _walk(root: Path, recursive: bool, follow_symlinks: bool):
    if not recursive:
        for p in root.iterdir():
            yield p
        return
    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=follow_symlinks
    ):
        for name in filenames:
            yield Path(dirpath) / name


def _format_report(groups: list[dict]) -> str:
    if not groups:
        return "No duplicates found."
    lines = [f"Found {len(groups)} duplicate group(s):"]
    saveable = 0
    for g in groups:
        copies = len(g["paths"])
        waste = g["size"] * (copies - 1)
        saveable += waste
        lines.append(f"  · {copies} × {_human_bytes(g['size'])} "
                     f"(waste {_human_bytes(waste)}) — {g['hash'][:12]}")
        for p in g["paths"]:
            lines.append(f"      {p}")
    lines.append(f"\nReclaimable by removing duplicates: {_human_bytes(saveable)}.")
    return "\n".join(lines)


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n/1024**(0 if unit=='B' else ['B','KB','MB','GB','TB'].index(unit)):.1f}{unit}"
        n //= 1024
    return f"{n}B"


def _tool_entry(parameters: dict, **_kw: Any) -> str:
    root = parameters.get("root") or parameters.get("path")
    if not root:
        return "Parameter `root` (directory to scan) is required."
    recursive = bool(parameters.get("recursive", True))
    min_bytes = int(parameters.get("min_bytes", 1))
    try:
        groups = scan(root, recursive=recursive, min_bytes=min_bytes)
    except FileNotFoundError:
        return f"Path not found: {root}"
    return _format_report(groups)


TOOL = {
    "name": "file_dedupe",
    "description": (
        "Scan a directory for duplicate files (same byte content). Reports "
        "groups with their total reclaimable bytes. Read-only — deletes are "
        "the caller's responsibility and should go through file_controller."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "root":      {"type": "string",
                          "description": "Directory to scan."},
            "recursive": {"type": "boolean",
                          "description": "Walk subdirectories (default true)."},
            "min_bytes": {"type": "integer",
                          "description": "Ignore files smaller than this (default 1)."},
        },
        "required": ["root"],
    },
    "run": _tool_entry,
}
