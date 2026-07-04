"""Atomic JSON file read/write with backup recovery."""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def load_json(path: Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load JSON; on corrupt/empty primary file try ``.bak`` backup."""
    fallback = dict(default or {})
    for candidate in (path, Path(str(path) + ".bak")):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8").strip()
            if not text:
                log.warning("JSON file empty: %s", candidate)
                continue
            data = json.loads(text)
            if not isinstance(data, dict):
                log.warning("JSON root is not object: %s", candidate)
                continue
            if candidate != path:
                log.warning("Restored %s from backup (read-only)", path.name)
            return data
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            log.warning("JSON read failed %s: %s", candidate, exc)
    return fallback


def atomic_json_save(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically (tmp + replace) and keep a ``.bak`` copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    payload = json.dumps(data, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    try:
        shutil.copy2(path, Path(str(path) + ".bak"))
    except OSError as exc:
        log.warning("JSON backup failed for %s: %s", path.name, exc)
