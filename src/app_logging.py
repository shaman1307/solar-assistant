"""Daily rotating application logs under logs/YYYY/MM/YYYY-MM-DD.log."""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import BASE_DIR

WARSAW = ZoneInfo("Europe/Warsaw")
LOG_ROOT = BASE_DIR / "logs"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def log_path_for_date(day: date | str) -> Path:
    """Return logs/YYYY/MM/YYYY-MM-DD.log for a calendar day (Warsaw)."""
    if isinstance(day, str):
        day = date.fromisoformat(day)
    return LOG_ROOT / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.isoformat()}.log"


def parse_log_date(date_str: str) -> date | None:
    if not date_str or not _DATE_RE.match(date_str):
        return None
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        return None


class DailyDirFileHandler(logging.Handler):
    """Write to logs/YYYY/MM/YYYY-MM-DD.log; create year/month dirs as needed."""

    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self._current_key: str | None = None
        self._stream = None
        self.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))

    def _day_now(self) -> date:
        return datetime.now(WARSAW).date()

    def _ensure_stream(self, day: date) -> None:
        key = day.isoformat()
        if key == self._current_key and self._stream is not None:
            return
        self._close_stream()
        path = log_path_for_date(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = open(path, "a", encoding="utf-8", newline="\n")
        self._current_key = key

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
            self._current_key = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_stream(self._day_now())
            assert self._stream is not None
            msg = self.format(record)
            self._stream.write(msg + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            self._close_stream()
        finally:
            super().close()


def setup_app_file_logging(root: logging.Logger | None = None) -> Path:
    """Attach daily file handler to the root logger (idempotent)."""
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    logger = root or logging.getLogger()
    for h in logger.handlers:
        if isinstance(h, DailyDirFileHandler):
            return LOG_ROOT
    handler = DailyDirFileHandler()
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    return LOG_ROOT


def list_log_dates(*, limit: int = 366) -> list[str]:
    """ISO dates that have a log file, newest first."""
    if not LOG_ROOT.is_dir():
        return []
    dates: list[str] = []
    for year_dir in sorted(LOG_ROOT.iterdir(), reverse=True):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        for month_dir in sorted(year_dir.iterdir(), reverse=True):
            if not month_dir.is_dir() or not month_dir.name.isdigit():
                continue
            for path in sorted(month_dir.glob("????-??-??.log"), reverse=True):
                day = parse_log_date(path.stem)
                if day is None:
                    continue
                dates.append(day.isoformat())
                if len(dates) >= limit:
                    return dates
    return dates


def read_log_day(date_str: str, *, max_bytes: int = 2_000_000) -> dict:
    """Read one daily log file (tail-truncated if larger than max_bytes)."""
    day = parse_log_date(date_str)
    if day is None:
        return {
            "date": date_str,
            "exists": False,
            "error": "invalid_date",
            "text": "",
            "truncated": False,
            "path": None,
        }
    path = log_path_for_date(day)
    try:
        rel = str(path.relative_to(BASE_DIR))
    except ValueError:
        rel = str(path)
    if not path.is_file():
        return {
            "date": day.isoformat(),
            "exists": False,
            "error": None,
            "text": "",
            "truncated": False,
            "path": rel,
        }
    raw = path.read_bytes()
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[-max_bytes:]
    text = raw.decode("utf-8", errors="replace")
    if truncated:
        # Drop partial first line after byte-tail cut.
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1 :]
        text = f"… [truncated to last {max_bytes} bytes]\n" + text
    return {
        "date": day.isoformat(),
        "exists": True,
        "error": None,
        "text": text,
        "truncated": truncated,
        "path": rel,
    }
