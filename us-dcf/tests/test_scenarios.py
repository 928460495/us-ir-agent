from pathlib import Path

import us_verify as V

FIX = Path(__file__).parent / "fixtures" / "AAPL_DCF.xlsx"


def test_三档验算带回每股价值():
    sc = {}
    fails = V.verify_all_scenarios(str(FIX), quiet=True, values=sc)
    assert fails == []
    assert set(sc) == {"Bull", "Base", "Bear"} and all(v > 0 for v in sc.values())


def test_不给values时行为不变():
    assert V.verify_all_scenarios(str(FIX), quiet=True) == []


def test_read_inputs给出生效增速与毛利率():
    inp = V.read_inputs(str(FIX))
    assert len(inp["growth"]) == len(inp["gm"]) == inp["ny"]


def _build(tmp_path, cal):
    import us_build as B
    path = str(tmp_path / "AAPL_test_DCF.xlsx")
    B.build(cal, path)
    return path


def test_固件能直接建表并通过验算(tmp_path):
    from conftest import load_cal
    path = _build(tmp_path, load_cal())
    fails, dv = V.verify(path, quiet=True)
    assert fails == [] and dv["ps"] > 0


def test_判断项按三档写入(tmp_path):
    from conftest import load_cal
    cal = load_cal()
    g, m = cal["judg"]["growth"], cal["judg"]["gm"]
    cal["judg"]["growth_scen"] = {"bull": [x + .05 for x in g], "base": list(g), "bear": [x - .05 for x in g]}
    cal["judg"]["gm_scen"] = {"bull": [x + .01 for x in m], "base": list(m), "bear": [x - .01 for x in m]}
    path = _build(tmp_path, cal)
    sc = {}
    assert V.verify_all_scenarios(path, quiet=True, values=sc) == []
    assert sc["Bull"] > sc["Base"] > sc["Bear"]


def test_三档临时副本每次调用位置不同_并行估值不互相删文件(monkeypatch):
    import shutil
    seen = []
    real = shutil.copy
    monkeypatch.setattr(shutil, "copy", lambda src, dst: (seen.append(str(dst)), real(src, dst))[1])
    V.verify_all_scenarios(str(FIX), quiet=True)
    V.verify_all_scenarios(str(FIX), quiet=True)
    assert len(seen) == 4 and len(set(seen)) == 4


def test_历史余额不截两位小数_期初现金与假设页一致(tmp_path):
    """PLTR 2026-11-16：资产负债表历史列截到两位小数（1423.80），假设页是 1423.796；差约百万分之一，
    经利息收益传到净利润、EPS，越过 1e-6 阈值，验算失败 128 处、估值被判不可采信。"""
    from openpyxl import load_workbook

    from conftest import load_cal
    cal = load_cal()
    bal = cal["stmts"]["balance"] = cal["stmts"]["balance"].astype(float)
    last = bal.index[-1]
    for k, col, d in (("cash", "现金及等价物", 0.0037), ("sti", "短期投资", -0.0037)):   # 资产合计不变
        v = float(bal.loc[last, col]) + d
        bal.loc[last, col] = v
        cal["open"][k] = v
    path = _build(tmp_path, cal)
    wb = load_workbook(path)
    a, b = wb["1_Assumptions"], wb["3_Balance"]
    row = next(r for r in range(1, a.max_row + 1) if a.cell(r, 1).value == "现金及等价物")
    hist = [b.cell(6, c).value for c in range(2, 40) if isinstance(b.cell(6, c).value, (int, float))]
    assert hist[-1] == a.cell(row, 2).value
    fails, _ = V.verify(path, quiet=True)
    assert fails == []
