"""
SolarAssistant client — reads metrics and writes settings via SA local REST API.

Uses py_solar_assistant.DeviceClient which connects via HTTP Basic Auth:
  admin:<sa_web_password>

SA must have a web password configured (Configuration → Security).
Set the password in sa-config.yaml under sa.password.
The host should include the port if SA is not on port 80:
  sa.host: "localhost:8080"   (legacy nginx install; default is "localhost" on port 80)
  sa.password: "your_password"

All public functions return safe defaults when SA is unreachable,
so the app keeps running with cached/forecast data even if SA is offline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

_INVERTER_PREFIX = "inverter_1"
_SLOT_COUNT = 3
_WORK_MODE_DEFAULT_OPTIONS = (
    "On-grid",
    "Limit power to UPS load",
    "Limit power to home load",
    "AC coupling",
)
WORK_MODE_ON_GRID = "On-grid"
WORK_MODE_LIMIT_HOME_LOAD = "Limit power to home load"
WORK_MODE_VERIFY_TIMEOUT_S = 65.0
WORK_MODE_VERIFY_INTERVAL_S = 5.0
_SA_CLIENT_TIMEOUT_S = 35.0
_SA_TIMEOUT_S = 30
_SA_LOCK_WAIT_S = 2.0
_RULES_CACHE_TTL_S = 120
_METRICS_CACHE_TTL_S = 20
_RULES_GLOB = f"{_INVERTER_PREFIX}/*"
_LIVE_METRIC_TOPICS = (
    "battery_1/*",
    "total/pv_power",
    "total/load_power",
    "total/grid_power",
    "total/pv_energy",
    "total/load_energy",
    "total/grid_energy_in",
    "total/grid_energy_out",
    "total/battery_energy_in",
    "total/battery_energy_out",
)
# SA daily energy topics (used when present in API response).
_SA_ENERGY_TOPICS: dict[str, str] = {
    "pv_energy_today": "total/pv_energy",
    "load_energy_today": "total/load_energy",
    "grid_buy_energy": "total/grid_energy_in",
    "grid_sell_energy": "total/grid_energy_out",
    "battery_charged_today": "total/battery_energy_in",
    "battery_discharged_today": "total/battery_energy_out",
}
# Serialize all SA REST calls — beam.smp on Pi Zero cannot handle concurrent requests.
_sa_lock: asyncio.Lock | None = None
_rules_cache: dict[str, Any] | None = None
_rules_cache_ts: float = 0.0
_metrics_cache: dict[str, Any] | None = None
_metrics_cache_ts: float = 0.0
_rules_fetch_lock: asyncio.Lock | None = None


def _get_sa_lock() -> asyncio.Lock:
    global _sa_lock
    if _sa_lock is None:
        _sa_lock = asyncio.Lock()
    return _sa_lock


def _get_rules_fetch_lock() -> asyncio.Lock:
    global _rules_fetch_lock
    if _rules_fetch_lock is None:
        _rules_fetch_lock = asyncio.Lock()
    return _rules_fetch_lock


def invalidate_rules_cache() -> None:
    global _rules_cache, _rules_cache_ts
    _rules_cache = None
    _rules_cache_ts = 0.0


async def _acquire_sa_lock(wait_s: float = _SA_LOCK_WAIT_S) -> bool:
    try:
        await asyncio.wait_for(_get_sa_lock().acquire(), timeout=wait_s)
        return True
    except asyncio.TimeoutError:
        return False


def _release_sa_lock() -> None:
    lock = _get_sa_lock()
    if lock.locked():
        lock.release()


async def _get_metrics(client: Any, *topics: str) -> list[Any]:
    """One HTTP request per topic/glob; discovery off for smaller/faster payloads."""
    return await asyncio.wait_for(
        client.get_metrics(*topics, discovery=False),
        timeout=_SA_TIMEOUT_S,
    )


def _empty_rules() -> dict[str, Any]:
    return {
        "grid_charge_enabled": False,
        "grid_export_enabled": False,
        "charge_current_a": 0.0,
        "timed_charge_enabled": None,
        "timed_discharge_enabled": None,
        "charge_slots": [],
        "discharge_slots": [],
        "work_mode": None,
        "work_mode_options": list(_WORK_MODE_DEFAULT_OPTIONS),
        "sa_online": False,
    }

_DEFAULTS: dict[str, Any] = {
    "battery_soc": 50.0,
    "battery_power": 0.0,
    "pv_power": 0.0,
    "load_power": 0.0,
    "grid_power": 0.0,
    "pv_energy_today": 0.0,
    "load_energy_today": 0.0,
    "grid_buy_energy": 0.0,
    "grid_sell_energy": 0.0,
    "sa_online": False,
}


def _work_mode_topic(cfg: dict) -> str:
    return cfg["sa"]["settings"].get("work_mode", f"{_INVERTER_PREFIX}/work_mode")


def _work_mode_options(cfg: dict) -> list[str]:
    opts = cfg.get("sa", {}).get("work_mode_options")
    if isinstance(opts, list) and opts:
        return [str(o) for o in opts]
    return list(_WORK_MODE_DEFAULT_OPTIONS)


def _build_client(cfg: dict):
    """Instantiate DeviceClient from config."""
    from py_solar_assistant import DeviceClient  # type: ignore

    sa = cfg["sa"]
    host = sa.get("host", "localhost")
    password = sa.get("password") or None
    if not password:
        raise ValueError(
            "SA password is not configured. "
            "Set a password in SA (Configuration → Security) and add it to sa-config.yaml."
        )
    return DeviceClient(host, password=password, timeout=_SA_CLIENT_TIMEOUT_S)


def _parse_metrics(raw_metrics: list, topic_map: dict[str, str]) -> dict[str, Any]:
    """Convert py_solar_assistant Metric objects into a flat dict.

    Uses m.topic (the actual MQTT-style topic, e.g. 'battery_1/state_of_charge')
    as the lookup key, NOT the human-readable m.name ('State of charge').
    """
    by_topic: dict[str, Any] = {}
    for m in raw_metrics:
        try:
            by_topic[m.topic] = m.value
        except Exception:
            pass

    result: dict[str, Any] = {}
    for key, topic in topic_map.items():
        val = by_topic.get(topic)
        result[key] = float(val) if val is not None else _DEFAULTS.get(key, 0.0)

    for key, topic in _SA_ENERGY_TOPICS.items():
        val = by_topic.get(topic)
        if val is not None:
            result[key] = float(val)

    result["_sa_grid_meter"] = (
        "total/grid_energy_in" in by_topic and "total/grid_energy_out" in by_topic
    )
    return result


def _grid_power_w_from_sa(
    raw_w: float,
    pv_w: float,
    load_w: float,
    battery_w: float,
    *,
    sa_grid_meter: bool,
    epsilon: float = 40.0,
) -> float:
    """Live grid power (W): import negative, export positive.

    New SA builds expose signed ``total/grid_power`` (negative = export) and
    separate ``total/grid_energy_in/out`` daily meters. Legacy SRNE builds
    reported gross positive W only — sign from PV/load/battery balance.
    """
    raw = float(raw_w)
    if sa_grid_meter:
        return -raw if abs(raw) >= epsilon else 0.0
    if raw < -epsilon:
        return -raw
    return _signed_grid_power_w(raw, pv_w, load_w, battery_w, epsilon=epsilon)


def _signed_grid_power_w(
    grid_gross_w: float,
    pv_w: float,
    load_w: float,
    battery_w: float,
    *,
    epsilon: float = 40.0,
) -> float:
    """Sign live grid power from PV/load/battery balance (legacy SA / SRNE gross W)."""
    gross = abs(float(grid_gross_w))
    if gross < epsilon:
        return 0.0
    bat_in = max(float(battery_w), 0.0)
    bat_out = max(-float(battery_w), 0.0)
    net = float(load_w) + bat_in - float(pv_w) - bat_out
    if net > epsilon:
        return -gross
    if net < -epsilon:
        return gross
    raw = float(grid_gross_w)
    if raw < -epsilon:
        return raw
    return 0.0


async def get_live_metrics(cfg: dict, *, fresh: bool = False) -> dict[str, Any]:
    """Return current live metrics from SA (with online flag)."""
    global _metrics_cache, _metrics_cache_ts

    now = time.time()
    if (
        not fresh
        and _metrics_cache is not None
        and _metrics_cache_ts > now - _METRICS_CACHE_TTL_S
    ):
        return dict(_metrics_cache)

    topic_map: dict[str, str] = cfg["sa"]["metrics"]
    if not await _acquire_sa_lock():
        if _metrics_cache:
            return dict(_metrics_cache)
        return dict(_DEFAULTS)

    try:
        client = _build_client(cfg)
        async with client as c:
            raw = await _get_metrics(c, *_LIVE_METRIC_TOPICS)
        data = _parse_metrics(raw, topic_map)
        sa_grid_meter = bool(data.pop("_sa_grid_meter", False))
        data["grid_power"] = _grid_power_w_from_sa(
            data.get("grid_power", 0.0),
            data.get("pv_power", 0.0),
            data.get("load_power", 0.0),
            data.get("battery_power", 0.0),
            sa_grid_meter=sa_grid_meter,
        )
        data["sa_online"] = True
        _metrics_cache = dict(data)
        _metrics_cache_ts = time.time()
        return data
    except Exception as exc:
        log.warning("SA metrics fetch failed: %r", exc)
        if _metrics_cache:
            stale = dict(_metrics_cache)
            stale["sa_online"] = False
            return stale
        return dict(_DEFAULTS)
    finally:
        _release_sa_lock()


async def get_rules(cfg: dict, *, fresh: bool = False) -> dict[str, Any]:
    """Return current inverter rule/settings state and timer schedule slots."""
    global _rules_cache, _rules_cache_ts

    now = time.time()
    if not fresh and _rules_cache is not None and _rules_cache_ts > now - _RULES_CACHE_TTL_S:
        return _rules_cache

    async with _get_rules_fetch_lock():
        if not fresh and _rules_cache is not None and _rules_cache_ts > now - _RULES_CACHE_TTL_S:
            return _rules_cache

        settings: dict[str, str] = cfg["sa"]["settings"]
        if not await _acquire_sa_lock(wait_s=60.0 if fresh else _SA_LOCK_WAIT_S):
            if _rules_cache is not None:
                stale = dict(_rules_cache)
                stale["stale"] = True
                return stale
            return _empty_rules()

        try:
            client = _build_client(cfg)
            async with client as c:
                # Single glob — py-solar-assistant does one HTTP call per topic arg;
                # listing 40+ slot topics caused 40+ sequential requests and froze the app.
                raw = await _get_metrics(c, _RULES_GLOB)
            by_topic: dict[str, Any] = {m.topic: m.value for m in raw}
            schedule = _parse_timer_schedule(by_topic)
            work_topic = _work_mode_topic(cfg)
            work_mode_raw = by_topic.get(work_topic)
            work_mode = str(work_mode_raw).strip() if work_mode_raw is not None else None
            result = {
                "grid_charge_enabled": _truthy(by_topic.get(settings["grid_charge_switch"])),
                "grid_export_enabled": _truthy(by_topic.get(settings["grid_export_switch"])),
                "charge_current_a": _float(by_topic.get(settings["charge_current_limit"]), 0.0),
                "timed_charge_enabled": schedule["timed_charge_enabled"],
                "timed_discharge_enabled": schedule["timed_discharge_enabled"],
                "charge_slots": schedule["charge_slots"],
                "discharge_slots": schedule["discharge_slots"],
                "work_mode": work_mode,
                "work_mode_options": _work_mode_options(cfg),
                "sa_online": True,
            }
            _rules_cache = result
            _rules_cache_ts = time.time()
            return result
        except Exception as exc:
            log.warning("SA rules fetch failed: %r", exc)
            if _rules_cache is not None:
                stale = dict(_rules_cache)
                stale["sa_online"] = False
                stale["stale"] = True
                return stale
            return _empty_rules()
        finally:
            _release_sa_lock()


async def _write_metrics(cfg: dict, writes: list[tuple[str, str]], *, lock_wait_s: float = 90.0) -> None:
    """Write SA settings via WebSocket (REST POST returns 500 on some SA/SRNE builds)."""
    if not writes:
        return
    if not await _acquire_sa_lock(wait_s=lock_wait_s):
        raise TimeoutError("SolarAssistant busy")

    host = cfg["sa"]["host"]
    password = cfg["sa"]["password"]
    try:
        from py_solar_assistant import Options, connect

        sock = await asyncio.wait_for(
            connect(Options(local_ip=host, password=password)),
            timeout=10,
        )
        try:
            for topic, value in writes:
                log.info("SA write %s=%s", topic, value)
                await asyncio.wait_for(sock.set_setting(topic, value), timeout=25)
        finally:
            await sock.close()
    except Exception as ws_exc:
        log.warning("SA WebSocket write failed, trying REST: %r", ws_exc)
        client = _build_client(cfg)
        async with client as c:
            for topic, value in writes:
                await asyncio.wait_for(c.set_metric(topic, value), timeout=15)
    finally:
        _release_sa_lock()


def _build_schedule_writes(
    schedule: dict[str, Any],
    *,
    charge_slot_nums: tuple[int, ...] | None = None,
    discharge_slot_nums: tuple[int, ...] | None = None,
) -> list[tuple[str, str]]:
    """Build SA metric writes for timer schedule (skip read-only SRNE topics)."""
    if charge_slot_nums is None:
        charge_slot_nums = (1, 2, 3)
    if discharge_slot_nums is None:
        discharge_slot_nums = (1, 2, 3)
    p = _INVERTER_PREFIX
    writes: list[tuple[str, str]] = []
    if schedule.get("timed_charge_enabled") is not None:
        writes.append((f"{p}/timed_charge", _sa_switch(bool(schedule["timed_charge_enabled"]))))
    if schedule.get("timed_discharge_enabled") is not None:
        writes.append((f"{p}/timed_discharge", _sa_switch(bool(schedule["timed_discharge_enabled"]))))
    for slot in schedule.get("charge_slots", []):
        n = int(slot["slot"])
        if n not in charge_slot_nums:
            continue
        cap = int(round(_float(slot.get("capacity_pct"), 0)))
        if cap < 1:
            cap = 1
        pwr_w = int(round(_float(slot.get("power_kw"), 0) * 1000))
        writes.extend([
            (f"{p}/charge_start_slot_{n}", _time_to_sa(slot.get("from"))),
            (f"{p}/charge_end_slot_{n}", _time_to_sa(slot.get("to"))),
            (f"{p}/charge_battery_capacity_slot_{n}", str(cap)),
            (f"{p}/charge_battery_voltage_slot_{n}", str(_float(slot.get("voltage_v"), 57.6))),
            (f"{p}/charge_power_slot_{n}", str(pwr_w)),
            (f"{p}/charge_using_grid_slot_{n}", _slot_bool(slot.get("grid", True))),
            (f"{p}/charge_using_generator_slot_{n}", _slot_bool(slot.get("generator", False))),
        ])
    for slot in schedule.get("discharge_slots", []):
        n = int(slot["slot"])
        if n not in discharge_slot_nums:
            continue
        cap = int(round(_float(slot.get("capacity_pct"), 0)))
        if cap < 1:
            cap = 1
        pwr_w = int(round(_float(slot.get("power_kw"), 0) * 1000))
        writes.extend([
            (f"{p}/discharge_start_slot_{n}", _time_to_sa(slot.get("from"))),
            (f"{p}/discharge_end_slot_{n}", _time_to_sa(slot.get("to"))),
            (f"{p}/discharge_battery_capacity_slot_{n}", str(cap)),
            (f"{p}/discharge_battery_voltage_slot_{n}", str(_float(slot.get("voltage_v"), 42.0))),
            (f"{p}/discharge_power_slot_{n}", str(pwr_w)),
        ])
    return writes


async def set_grid_charging(cfg: dict, *, enabled: bool, power_kw: float = 0.0) -> bool:
    """Enable or disable SA grid-charging rule."""
    settings: dict[str, str] = cfg["sa"]["settings"]
    writes: list[tuple[str, str]] = [
        (settings["grid_charge_switch"], _grid_charge_switch(enabled)),
    ]
    if enabled and power_kw > 0:
        voltage_v: float = 48.0
        current_a = int((power_kw * 1000) / voltage_v)
        writes.append((settings["charge_current_limit"], str(current_a)))
    try:
        await _write_metrics(cfg, writes, lock_wait_s=90.0)
        log.info("Grid charging %s (%.1f kW)", "ON" if enabled else "OFF", power_kw)
        return True
    except Exception as exc:
        log.error("Failed to set grid charging: %r", exc)
        return False


async def set_grid_export(cfg: dict, *, enabled: bool) -> bool:
    """Enable or disable SA timed discharge (grid export schedule)."""
    settings: dict[str, str] = cfg["sa"]["settings"]
    try:
        await _write_metrics(
            cfg,
            [(settings["grid_export_switch"], _sa_switch(enabled))],
            lock_wait_s=90.0,
        )
        log.info("Grid export %s", "ON" if enabled else "OFF")
        return True
    except Exception as exc:
        log.error("Failed to set grid export: %r", exc)
        return False


async def set_work_mode(
    cfg: dict,
    mode: str,
    *,
    verify: bool = True,
    verify_timeout_s: float = WORK_MODE_VERIFY_TIMEOUT_S,
) -> bool:
    """Write inverter Work mode to SA; poll until SA applies it (SRNE can take 30–60s)."""
    value = str(mode).strip()
    if not value:
        return False
    topic = _work_mode_topic(cfg)
    try:
        await _write_metrics(cfg, [(topic, value)], lock_wait_s=90.0)
        if not verify:
            invalidate_rules_cache()
            log.info("Work mode write sent — %s (no verify)", value)
            return True

        deadline = time.monotonic() + max(verify_timeout_s, WORK_MODE_VERIFY_INTERVAL_S)
        while time.monotonic() < deadline:
            await asyncio.sleep(WORK_MODE_VERIFY_INTERVAL_S)
            invalidate_rules_cache()
            rules = await get_rules(cfg, fresh=True)
            after = rules.get("work_mode")
            if after == value:
                log.info("Work mode set to %s (SA confirmed)", value)
                return True
            log.info("Work mode pending — want %r, SA has %r", value, after)

        log.error("Work mode verify timeout after %.0fs — wanted %r", verify_timeout_s, value)
        return False
    except Exception as exc:
        log.error("Failed to set work mode: %r", exc)
        return False


async def set_timed_power_flags(
    cfg: dict,
    *,
    timed_charge_enabled: bool | None = None,
    timed_discharge_enabled: bool | None = None,
) -> bool:
    """Write SA Power tab toggles (timed charge / timed discharge only)."""
    p = _INVERTER_PREFIX
    writes: list[tuple[str, str]] = []
    if timed_charge_enabled is not None:
        writes.append((f"{p}/timed_charge", _sa_switch(bool(timed_charge_enabled))))
    if timed_discharge_enabled is not None:
        writes.append((f"{p}/timed_discharge", _sa_switch(bool(timed_discharge_enabled))))
    if not writes:
        return False
    try:
        await _write_metrics(cfg, writes, lock_wait_s=90.0)
        log.info(
            "Timed power flags — charge=%s discharge=%s",
            timed_charge_enabled,
            timed_discharge_enabled,
        )
        invalidate_rules_cache()
        return True
    except Exception as exc:
        log.error("Failed to set timed power flags: %r", exc)
        return False


async def set_timer_schedule(cfg: dict, schedule: dict[str, Any]) -> bool:
    """Write selected timer slot(s) to SA (from charge_slots / discharge_slots in body)."""
    settings: dict[str, str] = cfg["sa"]["settings"]
    charge_nums = tuple(int(s["slot"]) for s in schedule.get("charge_slots", []))
    discharge_nums = tuple(int(s["slot"]) for s in schedule.get("discharge_slots", []))
    if not charge_nums and not discharge_nums:
        log.info("Timer schedule write skipped — no charge/discharge slots in payload")
        return True
    writes = _build_schedule_writes(
        schedule,
        charge_slot_nums=charge_nums,
        discharge_slot_nums=discharge_nums,
    )
    if 1 in charge_nums and schedule.get("timed_charge_enabled"):
        pwr = float((schedule.get("charge_slots") or [{}])[0].get("power_kw", 0))
        writes.append((settings["grid_charge_switch"], _grid_charge_switch(True)))
        if pwr > 0:
            writes.append((settings["charge_current_limit"], str(int((pwr * 1000) / 48.0))))
    elif 1 in charge_nums:
        writes.append((settings["grid_charge_switch"], _grid_charge_switch(False)))
    try:
        await _write_metrics(cfg, writes, lock_wait_s=90.0)
        log.info("Timer schedule saved to SA (charge=%s discharge=%s).", charge_nums, discharge_nums)
        invalidate_rules_cache()
        return True
    except Exception as exc:
        log.error("Failed to save timer schedule: %r", exc)
        return False


async def apply_hourly_schedule_to_sa(cfg: dict, schedule: dict[str, Any]) -> bool:
    """Auto-sync: write slot 1 for enabled timed charge and/or discharge only."""
    slim: dict[str, Any] = {
        "timed_charge_enabled": bool(schedule.get("timed_charge_enabled")),
        "timed_discharge_enabled": bool(schedule.get("timed_discharge_enabled")),
        "charge_slots": [],
        "discharge_slots": [],
    }
    if slim["timed_charge_enabled"]:
        slim["charge_slots"] = [schedule.get("charge_slots", [{}])[0]]
    if slim["timed_discharge_enabled"]:
        slim["discharge_slots"] = [schedule.get("discharge_slots", [{}])[0]]
    if not slim["charge_slots"] and not slim["discharge_slots"]:
        log.info("Hourly schedule apply skipped — no timed charge/discharge enabled")
        return True
    ok = await set_timer_schedule(cfg, slim)
    if ok:
        log.info(
            "Hourly schedule applied — hour=%s action=%s charge=%s discharge=%s (slot 1)",
            schedule.get("target_hour"),
            schedule.get("planned_action"),
            schedule.get("timed_charge_enabled"),
            schedule.get("timed_discharge_enabled"),
        )
    return ok


async def apply_plan_to_sa(
    cfg: dict,
    sim: dict[str, Any],
) -> bool:
    """Push plan-derived timer schedule (+ grid charge permission) to SA."""
    schedule = sim.get("proposed_schedule") or {}
    if not await set_timer_schedule(cfg, schedule):
        return False

    charge_pwr = max(
        (float(s.get("power_kw", 0)) for s in schedule.get("charge_slots", [])),
        default=0.0,
    )
    await set_grid_charging(
        cfg,
        enabled=bool(schedule.get("timed_charge_enabled")) and charge_pwr > 0,
        power_kw=charge_pwr or 2.0,
    )
    return True


# ---------------------------------------------------------------------------
# Timer schedule (SA → Configuration → Timer schedule)
# ---------------------------------------------------------------------------

def _timer_schedule_topics() -> list[str]:
    p = _INVERTER_PREFIX
    topics = [f"{p}/timed_charge", f"{p}/timed_discharge"]
    for n in range(1, _SLOT_COUNT + 1):
        topics.extend([
            f"{p}/charge_start_slot_{n}",
            f"{p}/charge_end_slot_{n}",
            f"{p}/charge_battery_capacity_slot_{n}",
            f"{p}/charge_battery_voltage_slot_{n}",
            f"{p}/charge_power_slot_{n}",
            f"{p}/charge_using_grid_slot_{n}",
            f"{p}/charge_using_generator_slot_{n}",
            f"{p}/discharge_start_slot_{n}",
            f"{p}/discharge_end_slot_{n}",
            f"{p}/discharge_battery_capacity_slot_{n}",
            f"{p}/discharge_battery_voltage_slot_{n}",
            f"{p}/discharge_power_slot_{n}",
        ])
    return topics


def _parse_timer_schedule(by_topic: dict[str, Any]) -> dict[str, Any]:
    p = _INVERTER_PREFIX
    charge_slots: list[dict[str, Any]] = []
    discharge_slots: list[dict[str, Any]] = []

    for n in range(1, _SLOT_COUNT + 1):
        gen = _truthy(by_topic.get(f"{p}/charge_using_generator_slot_{n}"))
        grid_raw = by_topic.get(f"{p}/charge_using_grid_slot_{n}")
        charge_slots.append({
            "slot": n,
            "from": _time_hhmm(by_topic.get(f"{p}/charge_start_slot_{n}")),
            "to": _time_hhmm(by_topic.get(f"{p}/charge_end_slot_{n}")),
            "capacity_pct": _float(by_topic.get(f"{p}/charge_battery_capacity_slot_{n}"), 0.0),
            "voltage_v": _float(by_topic.get(f"{p}/charge_battery_voltage_slot_{n}"), 0.0),
            "power_w": int(round(_float(by_topic.get(f"{p}/charge_power_slot_{n}"), 0.0))),
            "power_kw": round(_float(by_topic.get(f"{p}/charge_power_slot_{n}"), 0.0) / 1000.0, 2),
            "grid": _truthy(grid_raw) if grid_raw is not None else not gen,
            "generator": gen,
        })
        discharge_slots.append({
            "slot": n,
            "from": _time_hhmm(by_topic.get(f"{p}/discharge_start_slot_{n}")),
            "to": _time_hhmm(by_topic.get(f"{p}/discharge_end_slot_{n}")),
            "capacity_pct": _float(by_topic.get(f"{p}/discharge_battery_capacity_slot_{n}"), 0.0),
            "voltage_v": _float(by_topic.get(f"{p}/discharge_battery_voltage_slot_{n}"), 0.0),
            "power_w": int(round(_float(by_topic.get(f"{p}/discharge_power_slot_{n}"), 0.0))),
            "power_kw": round(_float(by_topic.get(f"{p}/discharge_power_slot_{n}"), 0.0) / 1000.0, 2),
        })

    return {
        "timed_charge_enabled": _optional_bool(by_topic.get(f"{p}/timed_charge")),
        "timed_discharge_enabled": _optional_bool(by_topic.get(f"{p}/timed_discharge")),
        "charge_slots": charge_slots,
        "discharge_slots": discharge_slots,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _time_hhmm(val: Any) -> str:
    if val is None:
        return "00:00"
    s = str(val).strip()
    return s[:5] if len(s) >= 5 else s


def _time_to_sa(val: Any) -> str:
    """SA select options use HH:MM (not HH:MM:SS)."""
    return _time_hhmm(val)


def _sa_switch(enabled: bool) -> str:
    """SRNE switch metrics use payload_on/off 1/0."""
    return "1" if enabled else "0"


def _sa_bool(enabled: bool) -> str:
    return _sa_switch(enabled)


def _grid_charge_switch(enabled: bool) -> str:
    """inverter_1/grid_charge uses Enabled/Disabled."""
    return "Enabled" if enabled else "Disabled"


def _slot_bool(val: Any) -> str:
    return _sa_bool(_truthy(val))


def _truthy(val: Any) -> bool:
    return str(val).lower() in ("1", "true", "yes", "enable", "enabled", "on")


def _optional_bool(val: Any) -> bool | None:
    if val is None:
        return None
    return _truthy(val)


def _float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default
