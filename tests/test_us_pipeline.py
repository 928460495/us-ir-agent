import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from ir_agent import pipeline
from ir_agent.sources import edgar

FIX = Path(__file__).parent / "fixtures" / "edgar"
AS_OF = date(2025, 11, 15)

FMP_INCOME = [{"date": "2025-09-27", "symbol": "AAPL", "period": "FY", "filingDate": "2025-10-31",
               "revenue": 416161000000, "operatingIncome": 133050000000, "netIncome": 112010000000}]
FMP_BALANCE = [{"date": "2025-09-27", "symbol": "AAPL", "period": "FY", "filingDate": "2025-10-31",
                "totalAssets": 359241000000, "totalStockholdersEquity": 73733000000}]
FMP_QUOTE = [{"symbol": "AAPL", "price": 270.0, "marketCap": 270.0 * 14776353000,
              "timestamp": 1762905600}]                                     # 2025-11-12


class R:
    def __init__(self, js=None, status=200):
        self._js, self.status_code = js, status
        self.text = json.dumps(js)

    def json(self):
        return self._js

    def raise_for_status(self):
        pass


def _sec(url, headers=None, timeout=None, params=None):
    name = url.rsplit("/", 1)[-1]
    if name == "company_tickers_exchange.json":
        return R(json.loads((FIX / name).read_text()))
    kind = "submissions" if "/submissions/" in url else "companyfacts"
    return R(json.loads((FIX / f"{kind}_{name[3:13]}.json").read_text()))


FMP_DENIED = {"on": False}


def _route(url, params=None, timeout=None, headers=None):
    """edgar / fmp / us_quotes 共用同一个 requests 模块，补丁只能打一次，按域名分派。"""
    if "sec.gov" in url:
        return _sec(url)
    if "financialmodelingprep" in url and FMP_DENIED["on"]:
        return R("Premium", status=402)
    if url.endswith("/quote"):
        return R(FMP_QUOTE)
    if "tiingo" in url:
        return R([{"date": "2025-11-12T00:00:00+00:00", "close": 270.0}])
    rows = {"income-statement": FMP_INCOME, "balance-sheet-statement": FMP_BALANCE}
    return R(rows[url.rsplit("/", 1)[-1]] if params.get("period") == "annual" else [])


@pytest.fixture
def fake_all(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Tester tester@example.org")
    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.setenv("TIINGO_API_KEY", "t")
    monkeypatch.setattr(edgar.requests, "get", _route)
    FMP_DENIED["on"] = False
    yield
    FMP_DENIED["on"] = False


def test_美股代码走美股数据源(fake_all, tmp_path):
    r = pipeline.run("AAPL", 2025, AS_OF, snapshot_dir=tmp_path)
    assert r.financials.primary == "edgar" and r.period == "2025FY"
    rev = r.ledger.get("revenue", "2025FY", as_of=AS_OF)
    assert rev.unit == "美元" and rev.value == D("416161000000")


def test_FMP一致时交叉核对生效(fake_all, tmp_path):
    r = pipeline.run("AAPL", 2025, AS_OF, snapshot_dir=tmp_path)
    assert r.financials.cross_checked and not r.financials.disagreements_in("2025FY")


def test_FMP402时摘要写不可用(fake_all, tmp_path):
    FMP_DENIED["on"] = True
    r = pipeline.run("AAPL", 2025, AS_OF, snapshot_dir=tmp_path)
    s = r.financials.summary("2025FY")
    assert "交叉核对不可用" in s and "已交叉验证" not in s


def test_三表勾稽在美股数据上通过(fake_all, tmp_path):
    r = pipeline.run("AAPL", 2025, AS_OF, snapshot_dir=tmp_path)
    assert r.reconcile.ok and r.reconcile.checks_run >= 2


def test_市值来自股价乘EDGAR股数(fake_all, tmp_path):
    r = pipeline.run("AAPL", 2025, AS_OF, snapshot_dir=tmp_path)
    cap = r.ledger.get("market_cap", "2025-11-12", as_of=AS_OF)
    assert cap.value == D("270.0") * D("14776353000")
    assert cap.derived_from[1] == "shares_outstanding@2025FY"


def test_草稿可溯源率百分之百(fake_all, tmp_path):
    r = pipeline.run("AAPL", 2025, AS_OF, snapshot_dir=tmp_path)
    assert r.audit.ok
    assert "[[" in r.draft


def test_营业总收入补自营业收入(fake_all, tmp_path):
    r = pipeline.run("AAPL", 2025, AS_OF, snapshot_dir=tmp_path)
    f = r.ledger.get("revenue_total", "2025FY", as_of=AS_OF)
    assert f.value == D("416161000000") and f.derived_from == ("revenue@2025FY",)


def test_时点早于年报披露时报错不用抓取日顶替(fake_all, tmp_path):
    with pytest.raises(RuntimeError, match="2025FY"):
        pipeline.run("AAPL", 2025, date(2025, 10, 1), snapshot_dir=tmp_path)


def test_A股代码路径不变(monkeypatch, tmp_path):
    hit = []

    def cn(*a, **k):
        hit.append("cninfo")
        raise RuntimeError("stop")
    monkeypatch.setattr(pipeline.cninfo, "search_with_status", cn)
    monkeypatch.setattr(pipeline, "_run_us", lambda *a, **k: hit.append("us"))
    with pytest.raises(RuntimeError, match="stop"):
        pipeline.run("600183", 2025, AS_OF, snapshot_dir=tmp_path)
    assert hit == ["cninfo"]


def test_EDGAR无us_gaap事实时报错而不是让FMP当主源(fake_all, monkeypatch, tmp_path):
    # 审查 I3：报 IFRS 的外国发行人（20-F）
    from ir_agent.sources.edgar_facts import NoUsGaapFacts
    monkeypatch.setattr(edgar, "fetch_companyfacts", lambda cik, store: (
        {"cik": 320193, "facts": {"ifrs-full": {"Revenue": {"units": {"TWD": []}}}}}, "sid"))
    with pytest.raises(NoUsGaapFacts):
        pipeline.run("AAPL", 2025, AS_OF, snapshot_dir=tmp_path)


def test_us_gaap里没有美元事实时同样报错(fake_all, monkeypatch, tmp_path):
    from ir_agent.sources.edgar_facts import NoUsGaapFacts
    monkeypatch.setattr(edgar, "fetch_companyfacts", lambda cik, store: (
        {"cik": 320193, "facts": {"us-gaap": {"Revenues": {"units": {"EUR": [
            {"start": "2024-09-29", "end": "2025-09-27", "val": 1, "form": "10-K",
             "filed": "2025-10-31", "fy": 2025, "fp": "FY"}]}}}}}, "sid"))
    with pytest.raises(NoUsGaapFacts, match="美元"):
        pipeline.run("AAPL", 2025, AS_OF, snapshot_dir=tmp_path)
