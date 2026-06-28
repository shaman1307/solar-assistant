"""Config load: sa-config.yaml + optional local overlay."""

from pathlib import Path

import yaml

from src.config import LOCAL_CONFIG_PATH, _deep_merge, _load_yaml, load_config


def test_deep_merge_nested():
    base = {"sa": {"host": "localhost", "password": "x"}, "simulation": {"min_soc_pct": 15}}
    overlay = {"sa": {"host": "192.168.8.57"}}
    merged = _deep_merge(base, overlay)
    assert merged["sa"]["host"] == "192.168.8.57"
    assert merged["sa"]["password"] == "x"
    assert merged["simulation"]["min_soc_pct"] == 15


def test_load_config_merges_local_overlay(tmp_path, monkeypatch):
    main = tmp_path / "sa-config.yaml"
    local = tmp_path / "sa-config.local.yaml"
    main.write_text(
        yaml.dump({"sa": {"host": "localhost", "password": "secret"}, "battery": {"capacity_kwh": 43}}),
        encoding="utf-8",
    )
    local.write_text(yaml.dump({"sa": {"host": "192.168.8.57"}}), encoding="utf-8")

    monkeypatch.setattr("src.config.DEFAULT_CONFIG_PATH", main)
    monkeypatch.setattr("src.config.LOCAL_CONFIG_PATH", local)
    monkeypatch.delenv("SOLAR_CONFIG_PATH", raising=False)

    cfg = load_config()
    assert cfg["sa"]["host"] == "192.168.8.57"
    assert cfg["sa"]["password"] == "secret"
    assert cfg["battery"]["capacity_kwh"] == 43.0
