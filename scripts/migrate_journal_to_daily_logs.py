#!/usr/bin/env python3
"""Split smart.service journal into logs/YYYY/MM/YYYY-MM-DD.log."""

from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_ROOT = ROOT / "logs"
LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T\S+\s+\S+\s+\S+:\s?(?P<msg>.*)$"
)


def main() -> int:
    print(f"Exporting journalctl -u smart.service → {LOG_ROOT}", flush=True)
    proc = subprocess.run(
        ["journalctl", "-u", "smart.service", "--no-pager", "-o", "short-iso"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        # 1 = no entries in some journalctl versions
        print(proc.stderr or "journalctl failed", file=sys.stderr)
        return proc.returncode

    by_day: dict[str, list[str]] = defaultdict(list)
    skipped = 0
    for raw in proc.stdout.splitlines():
        m = LINE_RE.match(raw)
        if not m:
            skipped += 1
            continue
        day = m.group("date")
        msg = m.group("msg")
        by_day[day].append(msg if msg else raw)

    # Preserve any newer lines already written by DailyDirFileHandler today.
    for day, lines in list(by_day.items()):
        path = LOG_ROOT / day[:4] / day[5:7] / f"{day}.log"
        if not path.is_file():
            continue
        existing = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not existing:
            continue
        known = set(lines)
        extra = [ln for ln in existing if ln and ln not in known]
        if extra:
            lines.extend(extra)

    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    written = 0
    total_bytes = 0
    for day in sorted(by_day):
        path = LOG_ROOT / day[:4] / day[5:7] / f"{day}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(by_day[day]) + ("\n" if by_day[day] else "")
        path.write_text(text, encoding="utf-8", newline="\n")
        written += 1
        total_bytes += len(text.encode("utf-8"))
        print(f"  {path.relative_to(ROOT)}  {len(by_day[day])} lines  {len(text)} bytes")

    print(
        f"Done: {written} day files, {total_bytes} bytes total, skipped_lines={skipped}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
