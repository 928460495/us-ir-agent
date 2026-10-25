from datetime import date
from decimal import Decimal as D

from ir_agent import cycle
from ir_agent.citation import audit_bare_numbers
from ir_agent.ledger import Fact, FactLedger, Method

AS_OF = date(2026, 10, 25)


def _put(led, key, rows, unit="ratio"):
    for period, v in rows:
        led.put(Fact(key=key, value=D(v), unit=unit, period=period, as_of=date(2026, 10, 1),
                     source_id="fred", method=Method.REPORTED))


def early_cycle_ledger():
    led = FactLedger()
    _put(led, "macro.industrial_production_yoy",
         [("2026-04", "-0.010"), ("2026-05", "-0.004"), ("2026-06", "0.002"),
          ("2026-07", "0.008"), ("2026-08", "0.014"), ("2026-09", "0.020")])
    _put(led, "macro.hy_spread",
         [("2026-09-24", "0.045"), ("2026-09-25", "0.042"), ("2026-09-28", "0.040"),
          ("2026-09-29", "0.038"), ("2026-09-30", "0.036"), ("2026-10-01", "0.035")])
    _put(led, "macro.term_spread",
         [("2026-09-24", "0.006"), ("2026-09-25", "0.008"), ("2026-09-28", "0.009"),
          ("2026-09-29", "0.010"), ("2026-09-30", "0.011"), ("2026-10-01", "0.012")])
    _put(led, "macro.ppi_yoy", [("2026-08", "0.010"), ("2026-09", "0.012")])
    _put(led, "macro.corporate_profits_yoy", [("2026Q1S", "0.05"), ("2026Q2S", "0.08")])
    _put(led, "macro.inventory_sales_ratio", [("2026-08", "1.37"), ("2026-09", "1.35")], unit="x")
    return led


def test_美国指标判定早周期():
    v = cycle.assess(early_cycle_ledger(), AS_OF, us=True)
    assert v.phase is cycle.Phase.EARLY
    dims = {s.dimension: s for s in v.signals}
    assert set(dims) == {"活动", "信贷", "政策"}
    assert dims["信贷"].direction is cycle.Direction.UP          # 利差收窄 = 信用扩张


def test_利润与库存作补充维度呈现():
    v = cycle.assess(early_cycle_ledger(), AS_OF, us=True)
    shown = {s.dimension for s in v.complementary}
    assert {"通胀", "利润", "库存与销售"} <= shown
    assert any("未参与阶段打分" in m for m in v.missing)


def test_美股提示词只有占位符():
    p = cycle.assess(early_cycle_ledger(), AS_OF, us=True).for_prompt()
    assert "[[macro.hy_spread@2026-10-01]]" in p and "[[macro.corporate_profits_yoy@2026Q2S]]" in p
    assert audit_bare_numbers(p) == []


def test_A股路径不读美国指标():
    v = cycle.assess(early_cycle_ledger(), AS_OF)
    assert v.signals == [] and v.scores == {}


def test_登记表有企业利润与库存销售比():
    from ir_agent.sources import fred
    reg = {s.key: s for s in fred.US_MACRO}
    assert reg["macro.corporate_profits_yoy"].series_id == "CP" and reg["macro.corporate_profits_yoy"].freq == "Q"
    assert reg["macro.inventory_sales_ratio"].series_id == "ISRATIO" and reg["macro.inventory_sales_ratio"].unit == "x"
    assert fred.classify("macro.inventory_sales_ratio") == "滞后"


def test_周期加载按市场分流():
    from types import SimpleNamespace
    from ir_agent import full
    r = SimpleNamespace(code="AAPL", ledger=early_cycle_ledger(), period="2025FY")
    human, prompt, err = full._load_cycle(r, AS_OF)
    assert "早周期" in human and err == ""
