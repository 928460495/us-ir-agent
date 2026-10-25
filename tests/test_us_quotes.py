import json
from datetime import date
from decimal import Decimal as D

import pytest

from ir_agent.ledger import Fact, Method
from ir_agent.sources import us_quotes
from ir_agent.sources.snapshot import SnapshotStore

SHARES = Fact(key="shares_outstanding", value=D("14776353000"), unit="股", period="2025FY",
              as_of=date(2025, 10, 31), source_id="sec_cf", method=Method.REPORTED)


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path)


def get(facts, key):
    return next(f for f in facts if f.key == key)


def test_市值由股价乘EDGAR股数():
    q = us_quotes.Quote(price=D("262.24"), as_of=date(2026, 10, 23), source="fmp", source_id="q1")
    facts = us_quotes.build_quote_facts(q, SHARES)
    cap = get(facts, "market_cap")
    assert cap.value == D("262.24") * D("14776353000")
    assert cap.period == "2026-10-23" and cap.unit == "美元" and cap.method is Method.COMPUTED
    assert cap.derived_from == ("last_price@2026-10-23", "shares_outstanding@2025FY")
    assert get(facts, "last_price").source_id == "q1"


def test_没有股数只出股价不出市值():
    q = us_quotes.Quote(price=D("100"), as_of=date(2026, 10, 23), source="fmp", source_id="q1")
    assert [f.key for f in us_quotes.build_quote_facts(q, None)] == ["last_price"]


def raise_unavailable(*a, **k):
    raise us_quotes.QuoteUnavailable("不可用")


def test_FMP不可用时退到Tiingo(monkeypatch, store):
    monkeypatch.setattr(us_quotes, "_fmp_quote", raise_unavailable)
    monkeypatch.setattr(us_quotes, "_tiingo_close", lambda t, store: us_quotes.Quote(
        price=D("100"), as_of=date(2026, 10, 23), source="tiingo", source_id="t1"))
    facts = us_quotes.fetch_quote_us("CAT", store, shares=SHARES)
    assert get(facts, "last_price").value == D("100") and get(facts, "last_price").source_id == "t1"


def test_两源都不可用时报错而非沿用旧价(monkeypatch, store):
    monkeypatch.setattr(us_quotes, "_fmp_quote", raise_unavailable)
    monkeypatch.setattr(us_quotes, "_tiingo_close", raise_unavailable)
    with pytest.raises(us_quotes.QuoteUnavailable):
        us_quotes.fetch_quote_us("CAT", store)


def test_两源市值相差超过百分之二记不一致():
    assert us_quotes.compare_cap(D("100"), D("103")).status == "不一致"
    assert us_quotes.compare_cap(D("100"), D("101.5")).status == "一致"
    assert us_quotes.compare_cap(D("100"), None).status == "不可用"


def test_FMP市值偏离时留说明(monkeypatch, store):
    monkeypatch.setattr(us_quotes, "_fmp_quote", lambda t, store: us_quotes.Quote(
        price=D("100"), as_of=date(2026, 10, 23), source="fmp", source_id="f1",
        market_cap=D("100") * D("14776353000") * D("1.05")))
    notes = []
    us_quotes.fetch_quote_us("AAPL", store, shares=SHARES, notes=notes)
    assert any("不一致" in n for n in notes)


class R:
    def __init__(self, body, status=200):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


def test_FMP报价解析与快照不带key(monkeypatch, tmp_path):
    monkeypatch.setenv("FMP_API_KEY", "fmpsecret")
    body = [{"symbol": "AAPL", "price": 262.24, "marketCap": 3891000000000, "timestamp": 1761249600}]
    monkeypatch.setattr(us_quotes.requests, "get", lambda url, params=None, headers=None, timeout=None: R(body))
    q = us_quotes._fmp_quote("AAPL", SnapshotStore(tmp_path))
    assert q.price == D("262.24") and q.market_cap == D("3891000000000") and q.as_of == date(2025, 10, 23)
    assert all("fmpsecret" not in p.read_text(encoding="utf-8") for p in tmp_path.iterdir())


def test_FMP报价402记为不可用(monkeypatch, store):
    monkeypatch.setenv("FMP_API_KEY", "fmpsecret")
    monkeypatch.setattr(us_quotes.requests, "get", lambda *a, **k: R("Premium", status=402))
    with pytest.raises(us_quotes.QuoteUnavailable, match="402"):
        us_quotes._fmp_quote("CAT", store)


def test_Tiingo取最新收盘价且key不进URL(monkeypatch, tmp_path):
    monkeypatch.setenv("TIINGO_API_KEY", "tiingosecret")
    seen = []

    def get(url, params=None, headers=None, timeout=None):
        seen.append((url, params, headers))
        return R([{"date": "2026-10-23T00:00:00+00:00", "close": 391.2, "adjClose": 391.2}])
    monkeypatch.setattr(us_quotes.requests, "get", get)
    q = us_quotes._tiingo_close("BRK.B", SnapshotStore(tmp_path))
    assert q.price == D("391.2") and q.as_of == date(2026, 10, 23) and q.source == "tiingo"
    assert "tiingosecret" not in seen[0][0] and "brk-b" in seen[0][0]
    assert seen[0][2]["Authorization"] == "Token tiingosecret"
    assert all("tiingosecret" not in p.read_text(encoding="utf-8") for p in tmp_path.iterdir())


def test_缺Tiingo_key时不可用(monkeypatch, store):
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    with pytest.raises(us_quotes.QuoteUnavailable, match="TIINGO_API_KEY"):
        us_quotes._tiingo_close("CAT", store)
