"""同业行情：Tiingo IEX 批量一次取齐；市值 = 价 × 时点内最新封面股数（陈旧或缺失不算）。不联网。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D

import pytest
import requests

from ir_agent.ledger import Fact, Method
from ir_agent.sources import us_quotes
from ir_agent.sources.snapshot import SnapshotStore
from ir_agent.sources.us_quotes import QuoteUnavailable, fetch_peer_quotes, latest_shares, peer_market_cap

KEY = "tiingo-secret-123"
AS_OF = date(2026, 10, 26)


def _row(t, last, ts="2026-10-23T20:00:00+00:00"):
    return {"ticker": t.lower(), "tngoLast": last, "prevClose": last - 1, "timestamp": ts,
            "last": None, "lastSaleTimestamp": None}


class _R:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path)


def _fake(monkeypatch, handler):
    monkeypatch.setenv("TIINGO_API_KEY", KEY)
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params or {}), dict(headers or {})))
        return handler(params["tickers"].split(","))
    monkeypatch.setattr(us_quotes.requests, "get", get)
    return calls


def test_批量一次取齐_收盘价与纽约报价日(monkeypatch, store):
    calls = _fake(monkeypatch, lambda ts: _R([_row("AAPL", 347.725), _row("NVDA", 210.96)]))
    got, notes = fetch_peer_quotes(["AAPL", "NVDA"], store)
    assert got["AAPL"].price == D("347.725") and got["AAPL"].as_of == date(2026, 10, 23)
    assert got["NVDA"].source == "tiingo_iex" and notes == []
    assert len(calls) == 1 and KEY not in calls[0][0] and KEY not in json.dumps(calls[0][1])
    assert calls[0][2]["Authorization"] == f"Token {KEY}"


def test_UTC凌晨的时间戳按纽约日期算(monkeypatch, store):
    _fake(monkeypatch, lambda ts: _R([_row("AAPL", 1.0, ts="2026-10-24T01:30:00+00:00")]))
    got, _ = fetch_peer_quotes(["AAPL"], store)
    assert got["AAPL"].as_of == date(2026, 10, 23)


def test_超过一百只分两批(monkeypatch, store):
    calls = _fake(monkeypatch, lambda ts: _R([_row(t, 10.0) for t in ts]))
    got, _ = fetch_peer_quotes([f"T{i}" for i in range(150)], store)
    assert len(calls) == 2 and len(got) == 150


def test_缺价的标的逐只说明_一批失败不拖垮其他批(monkeypatch, store):
    def handler(ts):
        if "t0" in ts:                                      # 请求里代码是小写
            return _R({"detail": "error"}, status=500)
        return _R([_row(t, 10.0) for t in ts if t not in ("t150", "t151")] + [_row("T151", 0.0)])
    _fake(monkeypatch, handler)
    got, notes = fetch_peer_quotes([f"T{i}" for i in range(152)], store)
    assert "T0" not in got and "T100" in got
    assert sum("HTTP 500" in n for n in notes) == 100
    assert any(n.startswith("T150：") and "未返回" in n for n in notes)
    assert any(n.startswith("T151：") and "价格" in n for n in notes)


def test_缺key时整体不可用(monkeypatch, store):
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    with pytest.raises(QuoteUnavailable, match="TIINGO_API_KEY"):
        fetch_peer_quotes(["AAPL"], store)


def test_快照不含key(monkeypatch, tmp_path):
    _fake(monkeypatch, lambda ts: _R([_row("AAPL", 1.0)]))
    fetch_peer_quotes(["AAPL"], SnapshotStore(tmp_path))
    assert all(KEY not in p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json"))


def _shares(v, as_of):
    return Fact(key="shares_outstanding", value=D(v), unit="股", period="2026-10-01", as_of=as_of,
                source_id="s", method=Method.REPORTED)


def test_无封面股数或股数陈旧不算市值():
    q = us_quotes.Quote(price=D("10"), as_of=date(2026, 10, 23), source="tiingo_iex", source_id="x")
    cap, why = peer_market_cap([], q, AS_OF)
    assert cap is None and "封面" in why
    cap, why = peer_market_cap([_shares(100, date(2024, 1, 1))], q, AS_OF)
    assert cap is None and "超过一年" in why
    cap, why = peer_market_cap([_shares(100, date(2026, 8, 1)), _shares(90, date(2026, 11, 1))], q, AS_OF)
    assert cap == D("1000") and why == ""                    # 时点之后申报的股数不可见


def test_pipeline的在外股数规则与共用函数一致():
    from ir_agent import pipeline
    from ir_agent.ledger import FactLedger
    led = FactLedger()
    led.put(_shares(100, date(2024, 1, 1)))
    assert pipeline._latest_shares(led, AS_OF) == latest_shares([_shares(100, date(2024, 1, 1))], AS_OF)
