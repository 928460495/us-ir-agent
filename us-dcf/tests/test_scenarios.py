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
