"""Markdown + JSON report writers.

Both writers verify the file exists after writing and raise `MarketError`
on failure. Nothing is silently overwritten — the path layer gives every
report a unique ID.
"""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from ..exceptions import MarketError


def _coerce(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return _coerce(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _coerce(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_coerce(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    data = _coerce(payload)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, allow_nan=False)
            f.write("\n")
    except OSError as e:
        raise MarketError(f"failed to write JSON to {path}: {e}") from None
    if not path.exists() or path.stat().st_size == 0:
        raise MarketError(f"JSON report at {path} was not persisted")
    return path


def write_markdown(path: Path, markdown: str) -> Path:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(markdown)
    except OSError as e:
        raise MarketError(f"failed to write Markdown to {path}: {e}") from None
    if not path.exists() or path.stat().st_size == 0:
        raise MarketError(f"Markdown report at {path} was not persisted")
    return path
