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
