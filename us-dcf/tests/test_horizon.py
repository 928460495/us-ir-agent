"""十年预测期、费用率逐年判断、过渡期向稳态收敛、终值 Beta / ROIC 规则（spec 2026-11-16）。不联网。

PLTR 2026-11-16：费用率取近 3 年均值且五年不变（营业利润率 31.6% → 19%）、5 年后直接接永续、终值 ROIC 43%
永续化、Beta 1.75 永续不变 —— DCF 16.69 美元 vs 现价 167.99 美元。"""

from __future__ import annotations

import copy

import pytest
from openpyxl import load_workbook

import us_build as B
import us_verify as V
from conftest import load_cal


def cal10(**judg):
    cal = load_cal()
    proj = cal["periods"]["proj"]
    y0 = int(proj[0][2:6])
    cal["periods"]["proj"] = [f"FY{y0 + i}E" for i in range(10)]
    cal["meta"]["nyears"] = 10
    cal["judg"].update(judg)
    return cal


def _build(tmp_path, cal, name="t.xlsx"):
    p = str(tmp_path / name)
    B.build(cal, p)
    return p


def _rows(ws):
    return {ws.cell(r, 1).value: r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value}


OPEX = {"研发费用率": {"bull": [0.06] * 5, "base": [0.07] * 5, "bear": [0.08] * 5},
        "销售及管理费用率": {"bull": [0.05] * 5, "base": [0.06] * 5, "bear": [0.07] * 5}}
STEADY = {"稳态毛利率": {"bull": 0.50, "base": 0.47, "bear": 0.44},
          "稳态研发费用率": {"bull": 0.05, "base": 0.06, "bear": 0.07},
          "稳态销售及管理费用率": {"bull": 0.04, "base": 0.05, "bear": 0.06},
          "终值ROIC溢价": {"bull": 0.05, "base": 0.03, "bear": 0.01}}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("h")
    return _build(d, cal10(opex_scen=OPEX, steady=STEADY))


def test_十年列且第6到10年为公式(built):
    wb = load_workbook(built)
    hdr = [wb["2_Income"].cell(5, c).value for c in range(2, 40)]
    assert len([h for h in hdr if h and str(h).endswith("E")]) == 10
    a = wb["1_Assumptions"]; m = _rows(a)
    proj = [h for h in hdr if h and str(h).endswith("E")]
    for k in ("营收增速", "毛利率", "研发费用 / 营收", "销售及管理费用 / 营收"):
        assert not str(a.cell(m[f"{proj[4]} {k}"], 3).value).startswith("=")      # 第 5 年是判断输入
        for y in proj[5:]:
            assert str(a.cell(m[f"{y} {k}"], 3).value).startswith("="), (y, k)    # 第 6–10 年是公式


def test_写入费用率判断项三档(built):
    a = load_workbook(built)["1_Assumptions"]; m = _rows(a)
    y1 = [k for k in m if isinstance(k, str) and k.startswith("FY") and k.endswith(" 研发费用 / 营收")][0]
    assert [a.cell(m[y1], c).value for c in (2, 3, 4)] == [0.06, 0.07, 0.08]


def test_过渡期线性收敛到稳态_两个方向():
    up = V.expand_paths(dict(nj=5, ny=10, growth=[.3, .25, .2, .15, .1], gm=[.40] * 5,
                             rd=[.10] * 5, sga=[.20] * 5, capex=[.05] * 5, sbc=[.01] * 5,
                             steady=dict(gm=.45, rd=.06, sga=.30), g=.03))
    assert [round(x, 4) for x in up["gm"][4:]] == [.40, .41, .42, .43, .44, .45]
    assert [round(x, 4) for x in up["rd"][5:]] == [.092, .084, .076, .068, .06]       # 下降
    assert [round(x, 4) for x in up["sga"][5:]] == [.22, .24, .26, .28, .30]          # 上升
    assert up["capex"][5:] == [.05] * 5 and up["sbc"][9] == .01                       # 沿用第 5 年


def test_第10年增速等于永续增长(built):
    inp = V.read_inputs(built)
    full = V.expand_paths(inp)
    assert len(full["growth"]) == 10 and abs(full["growth"][9] - inp["dcf"]["永续增长率 g"]) < 1e-12


def test_终值用终值Beta与ROIC模式2(built):
    inp = V.read_inputs(built)
    D = inp["dcf"]
    assert D["终值 Beta（向 1 收敛）"] == 1.0 and D["终值 ROIC 处理（1=沿用末年 2=收敛至 WACC+溢价）"] == 2
    _rows_, dv = V.recompute(inp)
    t = inp["gen"]["有效所得税率"]
    w_term = ((1 - D["债务权重 D/(D+E)"]) * (D["无风险利率 Rf（10Y UST）"] + 1.0 * D["股权风险溢价 ERP"])
              + D["债务权重 D/(D+E)"] * (D["无风险利率 Rf（10Y UST）"] + D["信用利差"]) * (1 - t))
    assert abs(dv["wacc_term"] - w_term) < 1e-12
    assert abs(dv["roic"] - (w_term + 0.03)) < 1e-12                                 # Base 档溢价


def test_十年结构三档验算通过(built):
    fails, dv = V.verify(built, quiet=True)
    assert fails == [] and dv["ps"] > 0
    assert V.verify_all_scenarios(built, quiet=True) == []


def test_无判断项时按校准值建表仍可验算(tmp_path):
    p = _build(tmp_path, cal10())
    fails, _ = V.verify(p, quiet=True)
    assert fails == []


def test_AAPL固件新旧每股价值变化在合理带内(tmp_path):
    """成熟公司换十年 + 稳态结构，估值不应出现数量级跳变（反目标一）。
    旧口径：5 年、ROIC 沿用末年、Beta 永续不变；新口径：10 年、校准值作稳态、ROIC 收敛至 WACC+3%、终值 Beta 1。"""
    old_cal = load_cal()
    old_cal["dcf"]["roic_mode"] = 1
    _, old = V.verify(_build(tmp_path, old_cal, "old.xlsx"), quiet=True)
    _, new = V.verify(_build(tmp_path, cal10(), "new.xlsx"), quiet=True)
    assert 0.5 < new["ps"] / old["ps"] < 2.0, (old["ps"], new["ps"])


def test_蒙特卡洛零波动复现确定性结果(built):
    import us_mc as MC
    inp = V.read_inputs(built)
    P = MC.defaults({})
    for k in list(P):
        if k.startswith("sd_"):
            P[k] = 0.0
    P["n_paths"] = 10
    out = MC.simulate(inp, P)
    _, dv = V.recompute(inp)
    assert abs(float(out["ps"].mean()) - dv["ps"]) < 1e-6 * max(1.0, abs(dv["ps"]))


def test_盈亏平衡对费用率按路径平移(built):
    import us_breakeven as BE
    names = [d[0] for d in BE.DRIVERS]
    assert "研发费用 / 营收（判断期均值）" in names and "销售及管理费用 / 营收（判断期均值）" in names
    out = BE.compute(built)
    assert out
