"""美股三表勾稽：现金恒等式接上；同标签恒等的两项标「不适用」而非「缺数据」；FMP 自身能勾稽。

2026-10-26 AAPL 重跑 ① 发现：EDGAR 只执行了 2/5 项、FMP 0/5 项，跨源勾稽「有效源不足 2 个」。
"""

from datetime import date
from decimal import Decimal as D

from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.sources import fmp
from ir_agent.sources.edgar_facts import parse_companyfacts
from ir_agent.validate.reconcile import reconcile

from tests.test_edgar_facts import _x, doc
from tests.test_fmp_source import ROUTES, fake_http

AS_OF = date(2026, 10, 26)
CASH = "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"
CHG = "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect"


def _led(facts) -> FactLedger:
    led = FactLedger()
    for f in facts:
        led.put(f)
    return led


def _aapl_cash_doc():
    return doc({
        CASH: [_x(None, "2024-09-28", 29943000000, "2024-11-01", fy=2024),
               _x(None, "2025-09-27", 35934000000, "2025-10-31")],
        CHG: [_x("2024-09-29", "2025-09-27", 5991000000, "2025-10-31")],
        "Assets": [_x(None, "2025-09-27", 359241000000, "2025-10-31")],
        "Liabilities": [_x(None, "2025-09-27", 285508000000, "2025-10-31")],
        "StockholdersEquity": [_x(None, "2025-09-27", 73733000000, "2025-10-31")],
        "NetIncomeLoss": [_x("2024-09-29", "2025-09-27", 112010000000, "2025-10-31")],
    })


def test_现金恒等式_期初取上一财年末余额():
    fs = parse_companyfacts(_aapl_cash_doc(), "0927", "sid")
    led = _led(fs)
    assert led.get("cash_end", "2025FY", as_of=AS_OF).value == D("35934000000")
    assert led.get("cash_net_change", "2025FY", as_of=AS_OF).value == D("5991000000")
    begin = led.get("cash_begin", "2025FY", as_of=AS_OF)
    assert begin.value == D("29943000000") and begin.as_of == date(2024, 11, 1)
    assert begin.derived_from == ("cash_end@2024FY",)
    res = {c.name: c for c in reconcile(led, "2025FY", AS_OF).results}
    assert res["期末现金=期初+净增加额"].status == "passed"


def test_无非控制性权益与现金流起点同标签_标不适用而非缺数据():
    led = _led(parse_companyfacts(_aapl_cash_doc(), "0927", "sid"))
    rep = reconcile(led, "2025FY", AS_OF)
    res = {c.name: c for c in rep.results}
    assert res["净利润=归母+少数股东损益"].status == "n/a"
    assert "非控制性权益" in res["净利润=归母+少数股东损益"].detail
    assert res["利润表净利润=现金流量表起点"].status == "n/a"
    assert "同一" in res["利润表净利润=现金流量表起点"].detail
    assert rep.checks_run == 2                                # 资产恒等 + 现金恒等
    assert not rep.complete                                   # 毛利缺数照旧算覆盖不完整
    assert [c.status for c in rep.results].count("skipped") == 1
    assert "2 项不适用" in rep.summary()


def test_有非控制性权益的美股照常勾稽():
    fs = parse_companyfacts(doc({
        "ProfitLoss": [_x("2024-09-29", "2025-09-27", 105, "2025-10-31")],
        "NetIncomeLoss": [_x("2024-09-29", "2025-09-27", 100, "2025-10-31")],
        "NetIncomeLossAttributableToNoncontrollingInterest": [_x("2024-09-29", "2025-09-27", 5, "2025-10-31")],
    }), "0927", "sid")
    res = {c.name: c for c in reconcile(_led(fs), "2025FY", AS_OF).results}
    assert res["净利润=归母+少数股东损益"].status == "passed"


def test_A股缺数仍是跳过不是不适用():
    led = FactLedger()
    led.put(Fact(key="net_profit", value=D(80), unit="元", period="2025FY", as_of=AS_OF,
                 source_id="em", method=Method.REPORTED, currency="CNY"))
    res = {c.name: c for c in reconcile(led, "2025FY", AS_OF).results}
    assert res["利润表净利润=现金流量表起点"].status == "skipped"
    assert res["净利润=归母+少数股东损益"].status == "skipped"


CF_FY = [{"date": "2025-09-27", "symbol": "AAPL", "fiscalYear": "2025", "period": "FY",
          "filingDate": "2025-10-31", "cashAtEndOfPeriod": 35934000000,
          "cashAtBeginningOfPeriod": 29943000000, "netChangeInCash": 5991000000}]


def test_FMP取够字段后自身至少两项勾稽(monkeypatch, tmp_path):
    from ir_agent.sources.snapshot import SnapshotStore
    routes = dict(ROUTES)
    routes[("income-statement", "annual")] = [dict(r, costOfRevenue=r["revenue"] - 195201000000,
                                                   grossProfit=195201000000)
                                              for r in ROUTES[("income-statement", "annual")][:1]]
    routes[("balance-sheet-statement", "annual")] = [dict(ROUTES[("balance-sheet-statement", "annual")][0],
                                                          totalLiabilities=285508000000,
                                                          totalEquity=73733000000)]
    routes[("cash-flow-statement", "annual")] = CF_FY
    fake_http(monkeypatch, routes=routes)
    fs = fmp.fetch_statements_fmp("AAPL", SnapshotStore(tmp_path), "0927")
    keys = {(f.key, f.period) for f in fs}
    assert {("total_liabilities", "2025FY"), ("total_equity", "2025FY"), ("gross_profit", "2025FY"),
            ("cost_of_revenue", "2025FY"), ("cash_end", "2025FY"), ("cash_begin", "2025FY"),
            ("cash_net_change", "2025FY")} <= keys
    rep = reconcile(_led(fs), "2025FY", AS_OF)
    assert rep.checks_run >= 2 and rep.ok


def test_美股折现率输入不再报β未接入():
    from ir_agent import full
    from ir_agent.sources import fred

    class R:
        ledger, store = FactLedger(), None
    r = R()
    f = Fact(key="rate.ust_10y", value=D("0.042"), unit="ratio", period="2026-10-23",
             as_of=date(2026, 10, 24), source_id="fred", method=Method.REPORTED)
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(fred, "fetch_series_asof", lambda *a, **k: [f])
    try:
        human, _prompt, err = full._load_capm_us(r, AS_OF)
    finally:
        mp.undo()
    assert "β" not in err and "阶段 3" not in err
    assert "us-dcf" in human and "五年" in human
