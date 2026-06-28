"""Forecast chart SOC must match Energy arbitrage table."""

from datetime import timedelta

from src.influxdb import now_warsaw
from src.plan_simulation import extract_plan_soc_hourly, extract_plan_soc_q15


def test_plan_soc_q15_from_ea_rows_not_optimizer_q15():
    today = now_warsaw().strftime("%Y-%m-%d")
    tomorrow = (now_warsaw() + timedelta(days=1)).strftime("%Y-%m-%d")
    plan = {
        "plan_soc_q15": {
            "today": [40.0] * 96,
            "tomorrow": [40.0] * 96,
        },
        "history_rows": [
            {
                "plan_date": today,
                "hour": 10,
                "soc": 62.0,
                "q15": [
                    {"quarter": q, "soc": 60.0 + q * 0.5}
                    for q in range(4)
                ],
            },
        ],
        "rows": [
            {
                "plan_date": today,
                "hour": 14,
                "soc": 71.5,
                "q15": [
                    {"quarter": q, "soc": 70.0 + q * 0.5}
                    for q in range(4)
                ],
            },
            {"plan_date": tomorrow, "hour": 3, "soc": 55.0},
        ],
    }
    hourly = extract_plan_soc_hourly(plan)
    assert hourly["today"][10] == 62.0
    assert hourly["today"][14] == 71.5
    assert hourly["tomorrow"][3] == 55.0

    q15 = extract_plan_soc_q15(plan)
    assert q15["today"][10 * 4] == 60.0
    assert q15["today"][10 * 4 + 3] == 61.5
    assert q15["today"][14 * 4] == 70.0
    assert q15["today"][14 * 4 + 3] == 71.5
    assert q15["today"][10 * 4] != q15["today"][10 * 4 + 1]
    assert q15["tomorrow"][3 * 4] == 40.0
    assert q15["tomorrow"][3 * 4] != 55.0
