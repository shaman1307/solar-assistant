"""Atomic JSON store helpers."""

import json
from pathlib import Path

from src.json_store import atomic_json_save, load_json


def test_atomic_save_survives_empty_primary(tmp_path: Path):
    path = tmp_path / "cache.json"
    atomic_json_save(path, {"days": {"2026-07-01": {"pv": 1}}})

    path.write_text("", encoding="utf-8")
    data = load_json(path, default={"days": {}})
    assert "2026-07-01" in data.get("days", {})
    # Primary still empty until next explicit save — read path does not rewrite disk.
    assert path.read_text(encoding="utf-8") == ""


def test_load_returns_default_when_missing(tmp_path: Path):
    path = tmp_path / "missing.json"
    assert load_json(path, default={"ok": True}) == {"ok": True}
