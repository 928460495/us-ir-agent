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


def test_蒙特卡洛在锚定开启时自检通过_首年不加扰动(tmp_path):
    # 接入时发现：us_mc 从未处理首年锚定，关掉随机后复现不出确定性模型（124.56 vs 140.28）
    import us_mc as MC
    from conftest import load_cal
    cal = load_cal()
    path = _build(tmp_path, cal)
    hist = dict(sd_growth=cal["judg"]["g_stats"][1], sd_margin=cal["judg"]["gm_stats"][1],
                rho=cal.get("_rho", 0.0), tax_mean=cal["gen"]["tax"], beta_mean=cal["dcf"]["beta"])
    out = MC.run(path, hist=hist, n=2000, seed=7, quiet=True)
    assert out["p5"] < out["p50"] < out["p95"]


# ── 审查发现：季度按期间对位，满四季不锚定 ─────────────────────────────

def _g(quarters: dict, annual: dict) -> dict:
    """合成 companyfacts：quarters {(start, end): (营收, 成本)}，annual {end: (营收, 成本)}（百万美元）。"""
    rev, cog = [], []
    for (s, e), (r, c) in quarters.items():
        rev.append(dict(start=s, end=e, val=r * 1e6, form="10-Q", filed=e))
        cog.append(dict(start=s, end=e, val=c * 1e6, form="10-Q", filed=e))
    for e, (r, c) in annual.items():
        s = f"{int(e[:4])}-01-01"
        rev.append(dict(start=s, end=e, val=r * 1e6, form="10-K", filed=e))
        cog.append(dict(start=s, end=e, val=c * 1e6, form="10-K", filed=e))
    return {"Revenues": {"units": {"USD": rev}}, "CostOfRevenue": {"units": {"USD": cog}}}


Q24 = {("2024-01-01", "2024-03-31"): (100, 60), ("2024-04-01", "2024-06-30"): (110, 66),
       ("2024-07-01", "2024-09-30"): (120, 72)}
A = {"2023-12-31": (400, 240), "2024-12-31": (460, 276)}
ENDS = ["2023-12-31", "2024-12-31"]


def _anchor(quarters):
    import us_data as UD
    blk = UD.interim_block(_g(quarters, A), ENDS)
    return blk, C.anchor_interim(blk)


def test_正常情形去年第四季由全年推出():
    blk, (it, skip) = _anchor({**Q24, ("2025-01-01", "2025-03-31"): (110, 63)})
    assert blk["q_prev_rev"] == [100, 110, 120, 130] and skip is None and it["k"] == 1


def test_去年缺第二季时按期间对位_不错位成别的季度():
    q = {k: v for k, v in Q24.items() if k[0] != "2024-04-01"}
    q[("2024-10-01", "2024-12-31")] = (130, 78)                 # 10-K 单列了第四季
    blk, (it, skip) = _anchor({**q, ("2025-01-01", "2025-03-31"): (110, 63)})
    assert blk["q_prev_rev"][1] is None and blk["q_prev_rev"][3] == 130
    assert it["fy_rev"] is None and "Q2" in skip


def test_本年缺第二季时不拿第三季顶替():
    cur = {("2025-01-01", "2025-03-31"): (110, 63), ("2025-07-01", "2025-09-30"): (132, 79)}
    blk, (it, skip) = _anchor({**Q24, **cur})
    assert blk["q_cur_rev"] == [110, None, 132] and it["fy_rev"] is None and "Q2" in skip


def test_本年已满四季不锚定也不截断():
    cur = {(s.replace("2024", "2025"), e.replace("2024", "2025")): v for (s, e), v in Q24.items()}
    cur[("2025-10-01", "2025-12-31")] = (140, 84)
    blk, (it, skip) = _anchor({**Q24, **cur})
    assert it["fy_rev"] is None and "新年报" in skip
