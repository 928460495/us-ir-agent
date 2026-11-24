"""研发资本化时 NOPAT 同步调整（spec 2026-11-24-rd-capitalization §4.2）。不联网。

此前只在投入资本里加研发资本，NOPAT 仍是报表口径（注释理由「稳态下研发≈摊销」）—— 研发增长时
研发 > 摊销，ROIC 被低估。Damodaran：调整后 NOPAT = EBIT×(1−t) + 研发 − 摊销（研发全额抵扣，税盾保留）。"""

from __future__ import annotations

import pytest
from openpyxl import load_workbook

import us_build as B
import us_verify as V
from conftest import load_cal


def _cal(rd_cap=1, roic_mode=1):
    cal = load_cal()
    cal["dcf"].update(rd_cap=rd_cap, roic_mode=roic_mode)
    return cal


def _build(tmp_path, cal, name):
    p = str(tmp_path / name)
    B.build(cal, p)
    return p


def test_调整后NOPAT加回研发减摊销(tmp_path):
    p = _build(tmp_path, _cal(1, 1), "a.xlsx")
    inp = V.read_inputs(p)
    rows, dv = V.recompute(inp)
    tt = inp["dcf"]["终值所得税率 t_term"]
    last = rows[-1]
    assert dv["nopat_adj"] == pytest.approx(last["ebit"] * (1 - tt) + last["rd"] - last["rd_am"], rel=1e-12)
    assert last["rd"] > last["rd_am"]                       # AAPL 研发在增长
    assert dv["roic_calc"] == pytest.approx(dv["nopat_adj"] / dv["ic"], rel=1e-12)


def test_开关0时两口径相同(tmp_path):
    p = _build(tmp_path, _cal(0, 1), "b.xlsx")
    inp = V.read_inputs(p)
    rows, dv = V.recompute(inp)
    assert dv["nopat_adj"] == pytest.approx(rows[-1]["ebit"] * (1 - inp["dcf"]["终值所得税率 t_term"]), rel=1e-12)


def test_模式2每股价值不随开关变(tmp_path):
    ps = {}
    for sw in (1, 0):
        p = _build(tmp_path, _cal(sw, 2), f"c{sw}.xlsx")
        ps[sw] = V.recompute(V.read_inputs(p))[1]["ps"]
    assert ps[1] == pytest.approx(ps[0], rel=1e-12)


@pytest.mark.parametrize("sw,mode", [(1, 1), (0, 1), (1, 2)])
def test_验算通过(tmp_path, sw, mode):
    p = _build(tmp_path, _cal(sw, mode), f"v{sw}{mode}.xlsx")
    fails, dv = V.verify(p, quiet=True)
    assert fails == []


def test_比率页两行ROIC与生效行(tmp_path):
    p = _build(tmp_path, _cal(1, 1), "r.xlsx")
    ws = load_workbook(p)["7_Ratios"]
    labels = [ws.cell(r, 1).value for r in range(1, ws.max_row + 1)]
    assert "ROIC（报表口径）" in labels and "ROIC（研发资本化）" in labels
    assert "ROIC（税后 / 生效投入资本）" in labels


def test_蒙特卡洛零波动复现确定性结果(tmp_path):
    import us_mc as MC
    p = _build(tmp_path, _cal(1, 1), "m.xlsx")
    inp = V.read_inputs(p)
    P = MC.defaults({})
    for k in list(P):
        if str(k).startswith("sd_"):
            P[k] = 0.0
    P["n_paths"] = 10
    out = MC.simulate(inp, P)
    _, dv = V.recompute(inp)
    assert abs(float(out["ps"].mean()) - dv["ps"]) < 1e-6 * max(1.0, abs(dv["ps"]))


def test_旧工作簿按申报口径NOPAT验算():
    """仓库里的 AAPL_DCF.xlsx 生成于 11-24 之前（研发资本化开关已开、但没有「调整后 NOPAT」行）。"""
    from pathlib import Path
    fix = str(Path(__file__).parent / "fixtures" / "AAPL_DCF.xlsx")
    inp = V.read_inputs(fix)
    assert inp["rd_nopat"] is False
    rows, dv = V.recompute(inp)
    assert dv["nopat_adj"] == pytest.approx(rows[-1]["ebit"] * (1 - inp["dcf"]["终值所得税率 t_term"]), rel=1e-12)
    assert V.verify(fix, quiet=True)[0] == []
