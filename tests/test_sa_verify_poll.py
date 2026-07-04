"""Verify polling uses single-topic SA read (REST glob lags behind SA UI)."""

import asyncio
from unittest.mock import AsyncMock, patch

from src.sa_client import _wait_inverter_setting_confirmed


def test_wait_confirmed_on_first_single_topic_read():
    cfg = {"sa": {"host": "localhost", "password": "x", "settings": {}}}

    async def run():
        with patch("src.sa_client._read_inverter_setting", new_callable=AsyncMock) as read:
            read.return_value = "Grid export enabled"
            with patch("src.sa_client.asyncio.sleep", new_callable=AsyncMock):
                ok = await _wait_inverter_setting_confirmed(
                    cfg,
                    topic="inverter_1/battery_discharge_mode",
                    value="Grid export enabled",
                    label="Battery discharge mode",
                    verify_timeout_s=90.0,
                )
        assert ok is True
        read.assert_called_once()

    asyncio.run(run())


def test_wait_retries_until_topic_matches():
    cfg = {"sa": {"host": "localhost", "password": "x", "settings": {}}}

    async def run():
        with patch("src.sa_client._read_inverter_setting", new_callable=AsyncMock) as read:
            read.side_effect = ["Standby", "Standby", "UPS and home loads"]
            with patch("src.sa_client.asyncio.sleep", new_callable=AsyncMock):
                ok = await _wait_inverter_setting_confirmed(
                    cfg,
                    topic="inverter_1/battery_discharge_mode",
                    value="UPS and home loads",
                    label="Battery discharge mode",
                    verify_timeout_s=90.0,
                )
        assert ok is True
        assert read.call_count == 3

    asyncio.run(run())
