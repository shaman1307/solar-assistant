"""Day-long quarter-tick simulations guarding plan history integrity.

Instead of asserting single merge calls, these tests drive the same call
sequence the Pi runs in production — scheduler quarter merges, forced/window
full rebuilds racing them, restarts, slow sims crossing an hour boundary —
and after EVERY write check the invariants that were violated when history
hours silently disappeared from SQLite:

  I1. History covers exactly hours [0 .. current_hour-1] — no holes, no future.
  I2. Plan rows cover exactly [current_hour .. 23] for today.
  I3. No hour is both in history and in rows.
  I4. Once an hour is in history, its timer_schedule/action never change.
  I5. soc_blended (violet live-SOC highlight in the UI) is never present on
      history rows — otherwise two rows are highlighted at once.
"""

from __future__ import annotations

import copy
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.grid_config import merge_grid_defaults
from src.plan_cache_merge import attach_immutable_history, merge_incremental_plan

TZ = ZoneInfo("Europe/Warsaw")
TODAY = "2026-07-07"
SIM_DURATION_S = 6  # realistic Pi full-sim wall time


def _cfg() -> dict:
    cfg = {
        "battery": {"capacity_kwh": 32.0, "min_soc_pct": 10},
        "simulation": {"min_soc_pct": 16},
        "grid": {},
    }
    merge_grid_defaults(cfg)
    return cfg


CFG = _cfg()


def _mk_row(hour: int, *, source: str) -> dict:
    """Plan row as run_simulation produces it (timer marks the producing tick)."""
    return {
        "plan_date": TODAY,
        "hour": hour,
        "start": f"07-07-2026 {(hour + 1) % 24:02d}:00",
        "production": 1.0,
        "consumption": 0.5,
        "battery": 0.0,
        "bat_charge": 0.0,
        "bat_discharge": 0.0,
        "grid_import": 0.0,
        "grid_export": 0.0,
        "soc": 50.0,
        "timer_schedule": f"Dis {hour:02d}:00-{hour:02d}:45 5kW cap42%",
        "action": f"Plan@{source}",
        # run_simulation marks the in-progress hour's SOC as blended (live);
        # promotion to history must strip it (UI invariant I5).
        "soc_blended": True,
        "q15": [
            {"quarter": q, "production": 0.25, "consumption": 0.125,
             "soc": 50.0, "battery": 0.0, "grid_import": 0.0,
             "grid_export": 0.0, "from_actual": False}
            for q in range(4)
        ],
    }


def _mk_fresh(sim_now: datetime) -> dict:
    """Fresh sim payload: rows from sim_now.hour, meter history for earlier hours."""
    from_hour = sim_now.hour
    label = sim_now.strftime("%H:%M:%S")
    hist = []
    for h in range(from_hour):
        row = _mk_row(h, source=f"meters@{label}")
        row["timer_schedule"] = ""  # meters never carry timers
        row["action"] = "Meters"
        row["history_hour"] = True
        hist.append(row)
    return {
        "today_date": TODAY,
        "plan_from_hour": from_hour,
        "computed_at": sim_now.strftime("%Y-%m-%d %H:%M:%S"),
        "delta_kwh": 0.0,
        "history_rows": hist,
        "rows": [_mk_row(h, source=label) for h in range(from_hour, 24)],
        "totals": {},
    }


def _today_history_hours(plan: dict) -> list[int]:
    return sorted(
        int(r["hour"]) for r in plan.get("history_rows") or []
        if str(r.get("plan_date")) == TODAY
    )


def _today_row_hours(plan: dict) -> list[int]:
    return sorted(
        int(r["hour"]) for r in plan.get("rows") or []
        if str(r.get("plan_date")) == TODAY and r.get("start") != "TOTAL"
    )


class HistoryLedger:
    """Tracks first-seen history content to assert immutability (I4)."""

    def __init__(self) -> None:
        self.seen: dict[int, tuple[str, str]] = {}

    def check(self, plan: dict, context: str) -> None:
        for row in plan.get("history_rows") or []:
            hour = int(row["hour"])
            content = (str(row.get("timer_schedule") or ""), str(row.get("action") or ""))
            if hour in self.seen:
                assert self.seen[hour] == content, (
                    f"{context}: history hour {hour} mutated "
                    f"{self.seen[hour]} -> {content}"
                )
            else:
                self.seen[hour] = content


def _assert_invariants(plan: dict, wall_hour: int, context: str, ledger: HistoryLedger) -> None:
    hist = _today_history_hours(plan)
    rows = _today_row_hours(plan)
    assert hist == list(range(wall_hour)), (
        f"{context}: history {hist} != contiguous 0..{wall_hour - 1}"
    )
    assert rows == list(range(wall_hour, 24)), (
        f"{context}: rows {rows} != {wall_hour}..23"
    )
    assert not set(hist) & set(rows), f"{context}: hour in both history and rows"
    blended_hist = [
        int(r["hour"]) for r in plan.get("history_rows") or [] if r.get("soc_blended")
    ]
    assert not blended_hist, (
        f"{context}: history hours {blended_hist} still carry soc_blended — "
        f"UI would highlight live SOC on more than one row"
    )
    ledger.check(plan, context)


def _scheduler_tick(stored: dict, now: datetime, *, sim_delay_s: int = SIM_DURATION_S) -> dict:
    """One quarter refresh as hourly_plan_refresh runs it: fresh sim, then merge.

    `now` is captured at tick start; the fresh sim finishes sim_delay_s later
    and starts from the wall hour at THAT moment (may cross the hour boundary).
    """
    fresh = _mk_fresh(now + timedelta(seconds=sim_delay_s))
    return merge_incremental_plan(stored, fresh, now=now, metrics={}, cfg=CFG)


def _forced_rebuild(stored: dict | None, enter_now: datetime, *, sim_delay_s: int = SIM_DURATION_S) -> dict:
    """Full rebuild as build_plan_simulation runs it (?refresh=1 / window mismatch)."""
    result = _mk_fresh(enter_now + timedelta(seconds=sim_delay_s))
    attach_immutable_history(result, stored, now=enter_now)
    return result


def _day_start_plan() -> dict:
    """Midnight full rebuild (new day, empty history)."""
    return _forced_rebuild(None, datetime(2026, 7, 7, 0, 0, 2, tzinfo=TZ), sim_delay_s=0)


def test_clean_day_of_quarter_ticks_keeps_history_contiguous():
    """Baseline: 96 scheduler ticks, no interference — history grows 0..23."""
    ledger = HistoryLedger()
    stored = _day_start_plan()
    _assert_invariants(stored, 0, "00:00 rebuild", ledger)

    for hour in range(24):
        for minute in (0, 15, 30, 45):
            if hour == 0 and minute == 0:
                continue
            now = datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ)
            stored = _scheduler_tick(stored, now)
            _assert_invariants(stored, hour, f"tick {hour:02d}:{minute:02d}", ledger)

    assert _today_history_hours(stored) == list(range(23))


def test_forced_rebuild_straddling_hour_boundary_loses_nothing():
    """The exact Pi failure: UI ?refresh=1 enters at HH:59:5x, sim ends after :00."""
    ledger = HistoryLedger()
    stored = _day_start_plan()

    for hour in range(24):
        for minute in (0, 15, 30, 45):
            if hour == 0 and minute == 0:
                continue
            now = datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ)
            stored = _scheduler_tick(stored, now)
            _assert_invariants(stored, hour, f"tick {hour:02d}:{minute:02d}", ledger)

        # UI-forced rebuild entered 4s before the next hour; its sim finishes after.
        enter = datetime(2026, 7, 7, hour, 59, 56, tzinfo=TZ)
        stored = _forced_rebuild(stored, enter)
        wall_hour = min(hour + 1, 23)  # sim crossed into the next hour
        _assert_invariants(stored, wall_hour, f"straddle rebuild {hour:02d}:59:56", ledger)


def test_missed_hour_tick_after_restart_recovers_next_quarter():
    """Deploy/restart swallows the :00 tick — the :15 merge must still promote."""
    ledger = HistoryLedger()
    stored = _day_start_plan()

    for hour in range(24):
        for minute in (0, 15, 30, 45):
            if hour == 0 and minute == 0:
                continue
            if minute == 0 and hour in (8, 13, 14):  # restarts swallow these ticks
                continue
            now = datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ)
            stored = _scheduler_tick(stored, now)
            _assert_invariants(stored, hour, f"tick {hour:02d}:{minute:02d}", ledger)


def test_forced_rebuild_racing_every_hour_boundary():
    """Forced rebuild right around each :00 in every order vs the scheduler tick."""
    ledger = HistoryLedger()
    stored = _day_start_plan()

    for hour in range(1, 24):
        boundary = datetime(2026, 7, 7, hour, 0, 0, tzinfo=TZ)
        if hour % 2:
            # UI wins the lock first (entered just after :00), scheduler second.
            stored = _forced_rebuild(stored, boundary + timedelta(seconds=1))
            _assert_invariants(stored, hour, f"UI-first {hour:02d}:00", ledger)
            stored = _scheduler_tick(stored, boundary + timedelta(seconds=8))
            _assert_invariants(stored, hour, f"sched-second {hour:02d}:00", ledger)
        else:
            # Scheduler first, then a UI forced rebuild a few seconds later.
            stored = _scheduler_tick(stored, boundary + timedelta(seconds=2))
            _assert_invariants(stored, hour, f"sched-first {hour:02d}:00", ledger)
            stored = _forced_rebuild(stored, boundary + timedelta(seconds=20))
            _assert_invariants(stored, hour, f"UI-second {hour:02d}:00", ledger)
        for minute in (15, 30, 45):
            now = datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ)
            stored = _scheduler_tick(stored, now)
            _assert_invariants(stored, hour, f"tick {hour:02d}:{minute:02d}", ledger)


@pytest.mark.parametrize("seed", [1, 7, 42, 1337])
def test_randomized_day_with_forced_rebuilds_and_slow_sims(seed: int):
    """Fuzzed day: random forced rebuilds, sim durations and missed ticks.

    Any interleaving of writers must keep history contiguous and immutable.
    """
    rng = random.Random(seed)
    ledger = HistoryLedger()
    stored = _day_start_plan()

    for hour in range(24):
        for minute in (0, 15, 30, 45):
            if hour == 0 and minute == 0:
                continue
            # 10% of scheduler ticks are lost to restarts (never two :00 in a row).
            if rng.random() < 0.10:
                continue
            tick_s = rng.randint(0, 20)
            sim_s = rng.randint(2, 12)
            now = datetime(2026, 7, 7, hour, minute, tick_s, tzinfo=TZ)
            stored = _scheduler_tick(stored, now, sim_delay_s=sim_s)
            wall = (now + timedelta(seconds=0)).hour
            _assert_invariants(stored, max(wall, stored["plan_from_hour"]),
                               f"seed{seed} tick {hour:02d}:{minute:02d}:{tick_s}", ledger)

            # 30% chance a UI forced rebuild lands right after, possibly
            # straddling the next hour boundary.
            if rng.random() < 0.30:
                offset_s = rng.choice([5, 30, 300, 880])  # up to :14:40 into the quarter
                enter = now + timedelta(seconds=offset_s)
                sim_s2 = rng.randint(2, 12)
                if enter.day != 7:
                    continue
                stored = _forced_rebuild(stored, enter, sim_delay_s=sim_s2)
                wall2 = (enter + timedelta(seconds=sim_s2)).hour
                if enter.hour > wall2:  # crossed midnight — out of scope
                    continue
                _assert_invariants(stored, max(wall2, stored["plan_from_hour"]),
                                   f"seed{seed} rebuild {enter:%H:%M:%S}", ledger)


def test_history_survives_sqlite_wipe_via_meter_seed():
    """delete_plan mid-day: seed restores meters history and the day continues."""
    ledger = HistoryLedger()
    stored = _day_start_plan()
    for hour in range(10):
        for minute in (0, 15, 30, 45):
            if hour == 0 and minute == 0:
                continue
            stored = _scheduler_tick(stored, datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ))

    # config save wipes plan_latest, then forces a rebuild with existing=None
    stored = _forced_rebuild(None, datetime(2026, 7, 7, 10, 5, 0, tzinfo=TZ))
    seed_ledger = HistoryLedger()  # seed rebuilds history from meters — new baseline
    _assert_invariants(stored, 10, "post-wipe seed", seed_ledger)

    for hour in range(10, 24):
        for minute in (0, 15, 30, 45):
            if hour == 10 and minute == 0:
                continue
            now = datetime(2026, 7, 7, hour, minute, 2, tzinfo=TZ)
            stored = _scheduler_tick(stored, now)
            _assert_invariants(stored, hour, f"post-wipe tick {hour:02d}:{minute:02d}", seed_ledger)
