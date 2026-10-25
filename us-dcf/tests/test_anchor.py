import pytest

import us_calibrate as C


def test_规则_已公布季度用实际_其余按年初至今同比外推():
    q, why = C.quarterly_estimate([100, 110, 120, 130], [60, 66, 72, 78], [110, 121], [63, 69])
    assert why == "" and q["k"] == 2
    assert q["g_rev"] == pytest.approx(0.1)
    assert q["fy_rev"] == pytest.approx(110 + 121 + 120 * 1.1 + 130 * 1.1)
    g_c = (63 + 69) / (60 + 66) - 1                       # 成本用自己的同比
    assert q["fy_cogs"] == pytest.approx(63 + 69 + 72 * (1 + g_c) + 78 * (1 + g_c))


@pytest.mark.parametrize("prev,cur,frag", [
    ([100, None, 120, 130], [110], "Q2"),
    ([100, 110, 120, -5], [110], "Q4"),
    ([100, 110, 120, 130], [350], "超出"),
    ([100, 110, 120, 130], [], "没有已公布"),
])
def test_规则_不锚定时写明原因(prev, cur, frag):
    q, why = C.quarterly_estimate(prev, [50] * 4, cur, [55] * len(cur))
    assert q is None and frag in why


def test_同比范围与A股一致():
    assert C.G_RANGE == (-0.75, 2.0)


def test_季度数据组装_去年四季由全年减前三季推出():
    from us_data import single_quarters_for
    # 去年 Q1–Q3 有单季事实，Q4 由全年推出；本年已公布两季
    prev, cur = single_quarters_for(q_prev=[100, 110, 120], fy_prev=460, q_cur=[110, 121])
    assert prev == [100, 110, 120, 130] and cur == [110, 121]


def test_本年已满四季时不锚定():
    from us_data import single_quarters_for
    with pytest.raises(ValueError, match="新年报"):
        single_quarters_for(q_prev=[100, 110, 120], fy_prev=460, q_cur=[1, 2, 3, 4])


def _build(tmp_path, cal):
    import us_build as B
    path = str(tmp_path / "AAPL_anchor_DCF.xlsx")
    B.build(cal, path)
    return path


def test_建表后锚定由活公式实现且独立重算一致(tmp_path):
    import us_verify as V
    from conftest import load_cal
    cal = load_cal()
    it = cal["interim"]
    assert it["fy_rev"] and it["k"] == 3
    path = _build(tmp_path, cal)
    inp = V.read_inputs(path)
    assert inp["it"]["sw"] == 1 and inp["it"]["fy_rev"] == pytest.approx(it["fy_rev"])
    fails, _ = V.verify(path, quiet=True)
    assert fails == []
