"""美股行业生命周期与 GDP 弹性的参照系：名义 GDP 五年复合增速、按本司财年对齐的 GDP 同比。不联网。

AAPL 第六次 ① 实跑：生命周期「缺名义 GDP 参照，无法判定」，画像里没有 GDP 弹性 —— 美股 GDP
挂在季度标签（2026Q2S）上，而 A 股那套按 FY 标签取。"""

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.sources import fred

AS_OF = date(2026, 10, 26)


def _gdp(period, v, as_of):
    return Fact(key="macro.gdp_nominal", value=D(v), unit="美元", currency="USD", period=period,
                as_of=as_of, source_id="fred_gdp", method=Method.REPORTED)


def _quarters(y0, y1, base=100, g=D("1.05")):
    out, v = [], D(base)
    for y in range(y0, y1 + 1):
        for q in range(1, 5):
            out.append(_gdp(f"{y}Q{q}S", v * g ** (y - y0), date(y, q * 3, 28) if q < 4 else date(y + 1, 1, 28)))
    return out


def test_名义GDP五年复合增速_同季对同季():
    cagr = [f for f in fred.nominal_gdp_cagr(_quarters(2020, 2026)) if f.key == "macro.gdp_nominal_cagr"]
    by = {f.period: f for f in cagr}
    assert "2026Q2S" in by and abs(by["2026Q2S"].value - D("0.05")) < D("1e-9")
    assert "2024Q2S" not in by                                   # 五年前（2019Q2）没有
    assert by["2026Q2S"].method is Method.COMPUTED
    assert set(by["2026Q2S"].derived_from) == {"macro.gdp_nominal@2021Q2S", "macro.gdp_nominal@2026Q2S"}


def test_按本司财年对齐的GDP同比_九月财年取十月到九月四个季度():
    from ir_agent import full
    led = FactLedger()
    for f in _quarters(2022, 2025):
        led.put(f)
    r = SimpleNamespace(ledger=led, target_ends={"2025FY": date(2025, 9, 27), "2024FY": date(2024, 9, 28)})
    yoy = full._gdp_yoy_by_fy_us(r, ["2025FY", "2024FY"], AS_OF)
    # 2025FY 取 2024Q4..2025Q3，2024FY 取 2023Q4..2024Q3；每年 +5%，季度错开一季，同比仍是 5%
    assert abs(yoy["2025FY"] - D("0.05")) < D("0.0001") and abs(yoy["2024FY"] - D("0.05")) < D("0.0001")


def test_GDP季度不全的财年不给同比():
    from ir_agent import full
    led = FactLedger()
    for f in _quarters(2024, 2025):
        led.put(f)
    r = SimpleNamespace(ledger=led, target_ends={"2024FY": date(2024, 9, 28)})
    assert full._gdp_yoy_by_fy_us(r, ["2024FY"], AS_OF) == {}    # 上一财年只有 2023Q4 以后的季度
