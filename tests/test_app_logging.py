"""Daily log file layout and read helpers."""

from datetime import date
from pathlib import Path

from src.app_logging import (
    DailyDirFileHandler,
    LOG_ROOT,
    list_log_dates,
    log_path_for_date,
    read_log_day,
    setup_app_file_logging,
)


def test_log_path_layout(tmp_path, monkeypatch):
    monkeypatch.setattr("src.app_logging.LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr("src.app_logging.BASE_DIR", tmp_path)
    p = log_path_for_date(date(2026, 7, 17))
    assert p == tmp_path / "logs" / "2026" / "07" / "2026-07-17.log"


def test_daily_handler_writes_and_lists(tmp_path, monkeypatch):
    import logging

    monkeypatch.setattr("src.app_logging.LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr("src.app_logging.BASE_DIR", tmp_path)
    root = logging.getLogger("test_app_logging_daily")
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.propagate = False
    setup_app_file_logging(root)
    root.info("hello from test")
    for h in root.handlers:
        if isinstance(h, DailyDirFileHandler):
            h.flush()
            h.close()
    days = list_log_dates()
    assert days
    payload = read_log_day(days[0])
    assert payload["exists"] is True
    assert "hello from test" in payload["text"]
    assert Path(payload["path"]).name.endswith(".log")
