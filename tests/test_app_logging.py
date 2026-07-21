"""Daily log file layout and read helpers."""

import logging
from datetime import date, datetime, timezone
from pathlib import Path

from src.app_logging import (
    DailyDirFileHandler,
    WarsawFormatter,
    list_log_dates,
    log_path_for_date,
    make_log_formatter,
    read_log_day,
    setup_app_file_logging,
)


def test_log_path_layout(tmp_path, monkeypatch):
    monkeypatch.setattr("src.app_logging.LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr("src.app_logging.BASE_DIR", tmp_path)
    p = log_path_for_date(date(2026, 7, 17))
    assert p == tmp_path / "logs" / "2026" / "07" / "2026-07-17.log"


def test_warsaw_formatter_not_utc():
    # 2026-07-20 12:00:00 UTC == 14:00:00 CEST (Europe/Warsaw)
    ts = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="x",
        args=(),
        exc_info=None,
    )
    record.created = ts
    fmt = WarsawFormatter("%(asctime)s", datefmt="%Y-%m-%d %H:%M:%S")
    assert fmt.formatTime(record, "%Y-%m-%d %H:%M:%S") == "2026-07-20 14:00:00"


def test_daily_handler_writes_and_lists(tmp_path, monkeypatch):
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
            assert isinstance(h.formatter, WarsawFormatter)
            h.flush()
            h.close()
    days = list_log_dates()
    assert days
    payload = read_log_day(days[0])
    assert payload["exists"] is True
    assert "hello from test" in payload["text"]
    assert Path(payload["path"]).name.endswith(".log")
    assert isinstance(make_log_formatter(), WarsawFormatter)
