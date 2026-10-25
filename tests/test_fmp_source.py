import json
from datetime import date
from decimal import Decimal as D

import pytest

from ir_agent.ledger import Fact, Method
from ir_agent.sources import fmp, resolve
from ir_agent.sources.snapshot import SnapshotStore

KEY = "secretkey123"

INCOME_FY = [{"date": "2025-09-27", "symbol": "AAPL", "fiscalYear": "2025", "period": "FY",
              "filingDate": "2025-10-31", "revenue": 416161000000, "operatingIncome": 133050000000,
              "netIncome": 112010000000},
             {"date": "2024-09-28", "symbol": "AAPL", "fiscalYear": "2024", "period": "FY",
              "filingDate": "2024-11-01", "revenue": 391035000000, "operatingIncome": 123216000000,
              "netIncome": 93736000000}]
INCOME_Q = [{"date": "2025-03-29", "symbol": "AAPL", "fiscalYear": "2025", "period": "Q2",
             "filingDate": "2025-05-02", "revenue": 95359000000, "operatingIncome": 29589000000,
             "netIncome": 24780000000},
            {"date": "2024-12-28", "symbol": "AAPL", "fiscalYear": "2025", "period": "Q1",
             "filingDate": "2025-01-31", "revenue": 124300000000, "operatingIncome": 42832000000,
             "netIncome": 36330000000}]
BALANCE_FY = [{"date": "2025-09-27", "symbol": "AAPL", "fiscalYear": "2025", "period": "FY",
               "filingDate": "2025-10-31", "totalAssets": 359241000000,
               "totalStockholdersEquity": 73733000000}]
BALANCE_Q = [{"date": "2025-03-29", "symbol": "AAPL", "fiscalYear": "2025", "period": "Q2",
              "filingDate": "2025-05-02", "totalAssets": 331233000000,
              "totalStockholdersEquity": 66796000000}]


class R:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._body if not isinstance(self._body, str) else json.loads(self._body)


def fake_http(monkeypatch, status=200, routes=None, seen=None):
    monkeypatch.setenv("FMP_API_KEY", KEY)

    def get(url, params=None, timeout=None):
        if seen is not None:
            seen.append((url, dict(params or {})))
        if status != 200:
            return R(status, "Premium Query Parameter: 'Special Endpoint : This value set for 'symbol' is not available under your current subscription")
        kind = url.rsplit("/", 1)[-1]
        return R(200, (routes or {}).get((kind, params.get("period")), []))
    monkeypatch.setattr(fmp.requests, "get", get)


ROUTES = {("income-statement", "annual"): INCOME_FY, ("income-statement", "quarter"): INCOME_Q,
          ("balance-sheet-statement", "annual"): BALANCE_FY,
          ("balance-sheet-statement", "quarter"): BALANCE_Q}


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path)


def test_402报不可用(monkeypatch, store):
    fake_http(monkeypatch, status=402)
    with pytest.raises(fmp.FmpUnavailable, match="402"):
        fmp.fetch_statements_fmp("CAT", store, "1231")


def test_缺key报不可用且不发请求(monkeypatch, store):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(fmp.requests, "get", lambda *a, **k: called.append(1))
    with pytest.raises(fmp.FmpUnavailable, match="FMP_API_KEY"):
        fmp.fetch_statements_fmp("AAPL", store, "0927")
    assert called == []


def test_取值并按财年末标期(monkeypatch, store):
    fake_http(monkeypatch, routes=ROUTES)
    fs = {(f.key, f.period): f for f in fmp.fetch_statements_fmp("AAPL", store, "0927")}
    assert fs[("revenue", "2025FY")].value == D("416161000000")
    assert fs[("revenue", "2025FY")].as_of == date(2025, 10, 31)
    assert fs[("revenue", "2025Q1")].value == D("124300000000")          # Q1 单季 = Q1 累计
    assert ("revenue", "2025H1") not in fs                                 # Q2 单季不当累计比
    assert fs[("total_assets", "2025H1")].value == D("331233000000")     # 存量各期都比
    assert fs[("equity_attr_parent", "2025FY")].unit == "美元"


def test_key不落快照也不进错误信息(monkeypatch, tmp_path):
    fake_http(monkeypatch, routes=ROUTES)
    fmp.fetch_statements_fmp("AAPL", SnapshotStore(tmp_path), "0927")
    for p in tmp_path.iterdir():
        assert KEY not in p.read_text(encoding="utf-8")
    fake_http(monkeypatch, status=402)
    with pytest.raises(fmp.FmpUnavailable) as e:
        fmp.fetch_statements_fmp("CAT", SnapshotStore(tmp_path), "1231")
    assert KEY not in str(e.value)


def _f(key, period, v, as_of=date(2025, 10, 31)):
    return Fact(key=key, value=D(v), unit="美元", period=period, as_of=as_of,
                source_id="s", method=Method.REPORTED, currency="USD")


def raise_unavailable():
    raise fmp.FmpUnavailable("FMP 不可用：HTTP 402（标的不在免费档范围）")


def test_resolve把FMP失败记为未核对而不是一致():
    res = resolve.resolve([("edgar", lambda: [_f("revenue", "2025FY", 1)]),
                           ("fmp", raise_unavailable)], cross_check=True)
    assert res.primary == "edgar"
    assert not res.cross_checked
    assert any(a.source == "fmp" and a.status == "failed" for a in res.attempts)


def test_核对报告里出现不可用字样():
    res = resolve.resolve([("edgar", lambda: [_f("revenue", "2025FY", 1)]),
                           ("fmp", raise_unavailable)], cross_check=True)
    s = res.summary("2025FY")
    assert "不可用" in s and "一致" not in s and "已交叉验证" not in s


def test_两源没有可比对的科目期间时不算已核对():
    res = resolve.resolve([("edgar", lambda: [_f("revenue", "2025FY", 1)]),
                           ("fmp", lambda: [_f("revenue", "2025H1", 1)])], cross_check=True)
    assert not res.cross_checked and res.compared == 0
    assert "不可用" in res.summary("2025FY")


def test_真正比对过才算已核对():
    res = resolve.resolve([("edgar", lambda: [_f("revenue", "2025FY", 1)]),
                           ("fmp", lambda: [_f("revenue", "2025FY", 1)])], cross_check=True)
    assert res.cross_checked and res.compared == 1
    assert "已交叉验证" in res.summary("2025FY")


def test_主源多版本时用最新版本比对():
    primary = [_f("revenue", "2024FY", 90, as_of=date(2025, 3, 1)),     # 修订后
               _f("revenue", "2024FY", 100, as_of=date(2024, 11, 1))]   # 原始，列表里排后面
    res = resolve.resolve([("edgar", lambda: primary),
                           ("fmp", lambda: [_f("revenue", "2024FY", 90)])], cross_check=True)
    assert res.disagreements == []
