"""行情取分析时点当日收盘（2026-11-16 PLTR 第④步隔天运行：FMP 实时报价时间戳晚于分析时点，被时点守卫
剔除，相对估值整节缺失、市值核对失效）。不联网。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from ir_agent.sources import us_quotes
from ir_agent.sources.snapshot import SnapshotStore

ROWS = [{"date": "2026-11-12T00:00:00.000Z", "close": 170.1},
        {"date": "2026-11-13T00:00:00.000Z", "close": 167.99},
        {"date": "2026-11-16T00:00:00.000Z", "close": 163.92}]


@pytest.fixture
def tiingo(monkeypatch):
    monkeypatch.setenv("TIINGO_API_KEY", "k" * 20)
    seen = {}

    def http(url, what, **kw):
        seen.update(url=url, **kw)
        end = (kw.get("params") or {}).get("endDate")
        return [r for r in ROWS if end is None or r["date"][:10] <= end]
    monkeypatch.setattr(us_quotes, "_http", http)
    return seen


def test_分析时点是周末_取之前最后一个交易日收盘(tiingo, tmp_path):
    q = us_quotes._tiingo_close("PLTR", SnapshotStore(tmp_path), as_of=date(2026, 11, 15))
    assert q.price == D("167.99") and q.as_of == date(2026, 11, 13)
    assert tiingo["params"]["endDate"] == "2026-11-15"
    assert "Token" in tiingo["headers"]["Authorization"]
    assert "k" * 20 not in tiingo["url"] and "k" * 20 not in str(tiingo["params"])


def test_返回里混入晚于分析时点的行也不取(monkeypatch, tmp_path):
    monkeypatch.setenv("TIINGO_API_KEY", "x" * 20)
    monkeypatch.setattr(us_quotes, "_http", lambda url, what, **kw: ROWS)
    q = us_quotes._tiingo_close("PLTR", SnapshotStore(tmp_path), as_of=date(2026, 11, 15))
    assert q.as_of == date(2026, 11, 13)


def test_不传分析时点时照旧取最新(tiingo, tmp_path):
    q = us_quotes._tiingo_close("PLTR", SnapshotStore(tmp_path))
    assert q.as_of == date(2026, 11, 16) and "params" not in tiingo or not tiingo.get("params")


def test_分析时点早于今天_只走Tiingo():
    names = [n for n, _ in us_quotes.quote_sources("PLTR", date(2026, 11, 15), today=date(2026, 11, 17))]
    assert names == ["tiingo"]


def test_分析时点是今天或昨天_照旧先FMP():
    for today in (date(2026, 11, 15), date(2026, 11, 16)):
        names = [n for n, _ in us_quotes.quote_sources("PLTR", date(2026, 11, 15), today=today)]
        assert names == ["fmp_quote", "tiingo"]
