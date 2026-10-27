"""相对估值计算核心的美股扩展（计划 Task 6）：PE(NTM)、EV/Sales、按市场选倍数、同业滞后标注、
增速口径闸门。A 股的倍数种类与行为不变。不联网。"""

from __future__ import annotations

from decimal import Decimal as D

from ir_agent.relval import (KINDS, US_KINDS, comps_bands, implied_price, kinds_for, multiples,
                             regress)
from tests.test_relval import _growth_sample, _tgt, obs


def test_增速口径TTM三年复合不被判成一年():
    r = regress("pe_growth", _growth_sample(), _tgt(growth=D("0.2"), growth_basis="TTM 3y",
                                                     net_profit=D(1)), D(1))
    assert "一年" not in r.why and "口径" not in r.why


def test_A股倍数种类不变():
    assert kinds_for(_tgt(), us=False) == KINDS == ("pe", "pb", "ev_ebitda")


def test_美股盈利公司不加EV_Sales():
    ks = kinds_for(_tgt(net_profit=D(5)), us=True)
    assert "ev_sales" not in ks and "pe_ntm" in ks and set(ks) <= set(US_KINDS)


def test_美股亏损公司加用EV_Sales且PE不适用():
    t = _tgt(net_profit=D(-5), revenue=D(100))
    peers = [obs(code=f"P{i}", revenue=D(50 + i), net_profit=D(5)) for i in range(6)]
    bands, why = comps_bands(peers, t, D(10), kinds=kinds_for(t, us=True))
    assert bands["ev_sales"] is not None and bands["pe"] is None
    assert any(w.startswith("PE(TTM)") for w in why)


def test_EV_Sales隐含价按EV桥接回股权():
    t = obs(revenue=D(200), cash=D(5), debt=D(20), total_equity=D(60), equity_parent=D(50))
    # (2 × 200 − 20 + 5 − 10) / 10
    assert implied_price("ev_sales", D(2), t, D(10)) == (D(400) - 20 + 5 - 10) / 10
    assert multiples(obs(revenue=D(125)))["ev_sales"] == (D(100) + 20 - 5 + 10) / 125


def test_PE_NTM用每股价除NTM_EPS_隐含价不经股数():
    o = obs(price=D(30), eps_ntm=D(2))
    assert multiples(o)["pe_ntm"] == D(15)
    assert implied_price("pe_ntm", D(15), obs(eps_ntm=D("2.5")), D(999)) == D("37.5")
    assert multiples(obs(price=D(30), eps_ntm=D(-1)))["pe_ntm"] is None


def test_本司无一致预期时PE_NTM说明只给TTM():
    t = _tgt(eps_ntm=None)
    peers = [obs(code=f"P{i}", price=D(30), eps_ntm=D(2)) for i in range(6)]
    bands, why = comps_bands(peers, t, D(10), kinds=kinds_for(t, us=True))
    assert bands["pe_ntm"] is None
    assert any(w.startswith("PE(NTM)") and "一致预期" in w for w in why)


def test_同业滞后标注走lags():
    from ir_agent.relval import _screen
    tgt = _tgt(np_basis="TTM 截至 2026-06-27")
    peers = [obs(code=f"P{i}", np_basis=f"TTM 截至 2026-0{4 + i % 3}-30", growth=D(str(0.01 * i)),
                 lags={"np": "利润 TTM 截至 2026-05-01，早一季"} if i == 3 else {})
             for i in range(10)]
    _rows, _exc, flags = _screen("pe_growth", peers, tgt)
    assert flags == {"P3": "利润 TTM 截至 2026-05-01，早一季"}       # 不再按标签逐字比


def test_摘要按本次的倍数种类列出():
    from tests.test_relval import make_result
    res = make_result()
    assert "PE(NTM)" not in res.summary()
    res.comps["pe_ntm"] = res.comps["pe"]
    s = res.summary()
    assert "PE(NTM)" in s and "valuation.rel_pe_ntm_mult_mid" in s


def test_观测带TTM营收_与EBITDA同截止期():
    from datetime import date
    from ir_agent.ledger import Fact, FactLedger, Method
    from ir_agent.relval_data import obs_from_ledger
    led = FactLedger()

    def put(k, p, v, d):
        led.put(Fact(key=k, value=D(v), unit="美元", currency="USD", period=p, as_of=d,
                     source_id="s", method=Method.REPORTED))
    for p, d in (("2025FY", date(2025, 11, 1)), ("2025Q1-Q3", date(2025, 8, 1)), ("2026Q1-Q3", date(2026, 8, 1))):
        f = {"2025FY": 1, "2025Q1-Q3": D("0.7"), "2026Q1-Q3": D("0.8")}[p]
        put("revenue", p, 400 * f, d); put("operating_profit", p, 120 * f, d)
        put("depreciation_fixed_asset", p, 10 * f, d)
        put("net_profit_attr_parent", p, 100 * f, d); put("equity_attr_parent", p, 70, d)
        put("total_equity", p, 70, d); put("cash_and_equivalents", p, 30, d)
    o = obs_from_ledger("AAPL", "Apple", led, "2025FY", date(2026, 10, 26), D(4000))
    assert o.revenue == D(400) * (D("0.8") + 1 - D("0.7"))       # TTM 截至 2026Q1-Q3
    assert "2026Q1-Q3" in o.flow_basis


def test_本司倍数分母落账本并在摘要里点明是TTM不是年报():
    """AAPL ④ 实跑：研报拿年报利润去核 PE(TTM)，对不上，就把 PE 可比法整个排除 —— 分母没说清。"""
    from datetime import date
    from ir_agent.citation import audit_bare_numbers
    from ir_agent.relval import to_facts
    from tests.test_relval import make_result
    res = make_result()
    res.target.revenue, res.target.eps_ntm = D(500), D("2.5")
    res.target.code = "AAPL"
    facts = {f.key: f for f in to_facts(res, date(2026, 10, 26))}
    assert facts["valuation.rel_target_np"].value == res.target.net_profit
    assert facts["valuation.rel_target_np"].unit == "美元"
    assert facts["valuation.rel_target_ebitda"].value == res.target.ebitda
    assert facts["valuation.rel_target_revenue"].value == D(500)
    assert facts["valuation.rel_target_eps_ntm"].value == D("2.5")
    s = res.summary()
    assert "[[valuation.rel_target_np@" in s and "不是年报" in s
    line = next(x for x in s.splitlines() if "rel_target_np" in x)
    assert audit_bare_numbers(line) == []
