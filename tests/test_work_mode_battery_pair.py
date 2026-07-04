"""Work mode must set paired battery discharge mode and wait for SA confirm."""

import asyncio
from unittest.mock import AsyncMock, patch

from src.sa_client import (
    BATTERY_DISCHARGE_MODE_GRID_EXPORT,
    BATTERY_DISCHARGE_MODE_UPS_AND_HOME,
    BATTERY_DISCHARGE_WORK_MODE_PAIR,
    WORK_MODE_LIMIT_HOME_LOAD,
    WORK_MODE_ON_GRID,
    WORK_MODE_BATTERY_DISCHARGE_PAIR,
    ensure_paired_battery_discharge_mode,
    ensure_paired_work_mode_for_battery,
    set_battery_discharge_mode,
    set_work_mode,
)


def test_work_mode_battery_pair_mapping():
    assert WORK_MODE_BATTERY_DISCHARGE_PAIR[WORK_MODE_ON_GRID] == BATTERY_DISCHARGE_MODE_GRID_EXPORT
    assert WORK_MODE_BATTERY_DISCHARGE_PAIR[WORK_MODE_LIMIT_HOME_LOAD] == BATTERY_DISCHARGE_MODE_UPS_AND_HOME
    assert BATTERY_DISCHARGE_WORK_MODE_PAIR[BATTERY_DISCHARGE_MODE_UPS_AND_HOME] == WORK_MODE_LIMIT_HOME_LOAD


def test_ensure_work_mode_before_ups_home_battery():
    cfg = {"sa": {"host": "localhost", "password": "x", "settings": {}}}

    async def run():
        with patch("src.sa_client._read_inverter_setting", new_callable=AsyncMock) as read:
            read.return_value = WORK_MODE_ON_GRID
            with patch("src.sa_client._set_inverter_enum_setting", new_callable=AsyncMock) as apply:
                apply.return_value = True
                ok = await ensure_paired_work_mode_for_battery(
                    cfg, BATTERY_DISCHARGE_MODE_UPS_AND_HOME,
                )
        assert ok is True
        apply.assert_called_once()
        assert apply.call_args.kwargs["value"] == WORK_MODE_LIMIT_HOME_LOAD

    asyncio.run(run())


def test_ensure_paired_skips_when_already_set():
    cfg = {"sa": {"host": "localhost", "password": "x", "settings": {}}}

    async def run():
        with patch("src.sa_client._read_inverter_setting", new_callable=AsyncMock) as read:
            read.return_value = BATTERY_DISCHARGE_MODE_GRID_EXPORT
            with patch("src.sa_client._set_inverter_enum_setting", new_callable=AsyncMock) as apply:
                ok = await ensure_paired_battery_discharge_mode(cfg, WORK_MODE_ON_GRID)
        assert ok is True
        apply.assert_not_called()

    asyncio.run(run())


def test_ensure_paired_writes_when_mismatch():
    cfg = {"sa": {"host": "localhost", "password": "x", "settings": {}}}

    async def run():
        with patch("src.sa_client._read_inverter_setting", new_callable=AsyncMock) as read:
            read.return_value = "Standby"
            with patch("src.sa_client._set_inverter_enum_setting", new_callable=AsyncMock) as apply:
                apply.return_value = True
                ok = await ensure_paired_battery_discharge_mode(cfg, WORK_MODE_ON_GRID)
        assert ok is True
        apply.assert_called_once()
        call_kw = apply.call_args.kwargs
        assert call_kw["value"] == BATTERY_DISCHARGE_MODE_GRID_EXPORT

    asyncio.run(run())


def test_set_battery_discharge_sets_work_mode_first():
    cfg = {"sa": {"host": "localhost", "password": "x", "settings": {}}}

    async def run():
        with patch("src.sa_client.ensure_paired_work_mode_for_battery", new_callable=AsyncMock) as wm:
            wm.return_value = True
            with patch("src.sa_client._set_inverter_enum_setting", new_callable=AsyncMock) as apply:
                apply.return_value = True
                ok = await set_battery_discharge_mode(cfg, BATTERY_DISCHARGE_MODE_UPS_AND_HOME)
        assert ok is True
        wm.assert_called_once_with(cfg, BATTERY_DISCHARGE_MODE_UPS_AND_HOME)
        apply.assert_called_once()

    asyncio.run(run())


def test_set_work_mode_chains_paired_battery_on_grid():
    cfg = {"sa": {"host": "localhost", "password": "x", "settings": {}}}

    async def run():
        with patch("src.sa_client._set_inverter_enum_setting", new_callable=AsyncMock) as set_enum:
            set_enum.return_value = True
            with patch("src.sa_client.ensure_paired_battery_discharge_mode", new_callable=AsyncMock) as ensure:
                ensure.return_value = True
                ok = await set_work_mode(cfg, WORK_MODE_ON_GRID)
        assert ok is True
        ensure.assert_called_once_with(cfg, WORK_MODE_ON_GRID)

    asyncio.run(run())


def test_set_work_mode_fails_if_battery_pair_fails():
    cfg = {"sa": {"host": "localhost", "password": "x", "settings": {}}}

    async def run():
        with patch("src.sa_client._set_inverter_enum_setting", new_callable=AsyncMock) as set_enum:
            set_enum.return_value = True
            with patch("src.sa_client.ensure_paired_battery_discharge_mode", new_callable=AsyncMock) as ensure:
                ensure.return_value = False
                ok = await set_work_mode(cfg, WORK_MODE_LIMIT_HOME_LOAD)
        assert ok is False

    asyncio.run(run())


def test_set_work_mode_skips_battery_when_work_mode_write_fails():
    cfg = {"sa": {"host": "localhost", "password": "x", "settings": {}}}

    async def run():
        with patch("src.sa_client._set_inverter_enum_setting", new_callable=AsyncMock) as set_enum:
            set_enum.return_value = False
            with patch("src.sa_client.ensure_paired_battery_discharge_mode", new_callable=AsyncMock) as ensure:
                ok = await set_work_mode(cfg, WORK_MODE_ON_GRID)
        assert ok is False
        ensure.assert_not_called()

    asyncio.run(run())
