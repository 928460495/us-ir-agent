from datetime import date
from decimal import Decimal as D

import pytest

from ir_agent import dcf, market, relval
from ir_agent.citation import format_value
from ir_agent.ledger import Fact, Method

AS_OF = date(2026, 10, 25)


def fact(unit, value):
    return Fact(key="k", value=value, unit=unit, period="2025FY", as_of=AS_OF,
                source_id="s", method=Method.REPORTED)


@pytest.mark.parametrize("v,shown", [(D("12345000000"), "123.45 亿美元"),
                                    (D("54321000"), "5432.10 万美元"),
                                    (D("347.61"), "347.61 美元"),
                                    (D("-2500000000"), "-25.00 亿美元")])
def test_美元渲染(v, shown):
    assert format_value(fact("美元", v)) == shown


def test_股数渲染():
    assert format_value(fact("股", D("14776353000"))) == "147.76 亿股"


@pytest.mark.parametrize("code,us", [("600183", False), ("000001", False), ("AAPL", True),
                                    ("brk.b", True), ("BRK-B", True), (" 600183 ", False)])
def test_按代码形状分流(code, us):
    assert market.is_us(code) is us
    assert market.money_unit(code) == ("美元" if us else "元")


def _dcf(code):
    return dcf.DcfResult(code=code, workbook="/tmp/x.xlsx", price=D("38"), value_per_share=D("45.6"),
                         wacc=D("0.09"), terminal_roic=D("0.11"), reinvest_rate=D("0.42"),
                         terminal_share=D("0.68"), verified=True,
                         scenario_ps={"bear": D(30), "base": D(45), "bull": D(60)})


def _money_units(facts):
    return {f.unit for f in facts if f.unit in ("元", "美元")}


def test_美股代码的估值事实用美元():
    assert _money_units(dcf.to_facts(_dcf("AAPL"), AS_OF)) == {"美元"}
    assert {f.unit for f in dcf.to_facts(_dcf("AAPL"), AS_OF)
            if f.key == "valuation.value_per_share"} == {"美元"}


def test_A股估值事实仍用元():
    assert _money_units(dcf.to_facts(_dcf("600183"), AS_OF)) == {"元"}


def _relval(code):
    tgt = relval.PeerObs(code=code, name="X", market_cap=D(100), net_profit=D(10), np_basis="TTM",
                         equity_parent=D(50), total_equity=D(50), debt=D(0), cash=D(0),
                         ebitda=D(20), roe=D("0.2"), growth=D("0.1"), growth_basis="3y")
    band = relval.Band(lo=D(1), mid=D(2), hi=D(3), mult_lo=D(1), mult_mid=D(2), mult_hi=D(3), n=6)
    return relval.RelvalResult(as_of=AS_OF, price=D(2), shares=D(1), target=tgt,
                               comps={"pe": band}, comps_notes=[], regs={}, sample_size=0,
                               industry="X")


def test_相对估值事实按代码取单位():
    assert _money_units(relval.to_facts(_relval("AAPL"), AS_OF)) == {"美元"}
    assert _money_units(relval.to_facts(_relval("600183"), AS_OF)) == {"元"}
