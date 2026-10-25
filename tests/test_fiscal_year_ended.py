"""非自然年财年的最新一年要被当作「已结束」（审查 I1）。"""
from datetime import date
from decimal import Decimal as D

from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.operators.ratios import annual_periods


def _led(*rows):
    led = FactLedger()
    for key, period, filed in rows:
        led.put(Fact(key=key, value=D(1), unit="美元", currency="USD", period=period,
                     as_of=filed, source_id="s", method=Method.REPORTED))
    return led


def test_苹果九月财年申报后即算已结束():
    led = _led(("revenue", "2024FY", date(2024, 11, 1)), ("revenue", "2025FY", date(2025, 10, 31)))
    assert annual_periods(led, date(2025, 11, 15)) == ["2024FY", "2025FY"]


def test_英伟达一月财年申报后即算已结束():
    led = _led(("total_assets", "2025FY", date(2025, 2, 26)), ("total_assets", "2026FY", date(2026, 2, 25)))
    assert annual_periods(led, date(2026, 6, 1)) == ["2025FY", "2026FY"]


def test_一致预期的未来年度仍不算已结束():
    led = _led(("revenue", "2025FY", date(2026, 3, 1)), ("consensus.eps", "2027FY", date(2026, 3, 1)))
    assert annual_periods(led, date(2026, 6, 1)) == ["2025FY"]
