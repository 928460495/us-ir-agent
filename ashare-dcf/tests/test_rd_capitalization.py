# -*- coding: utf-8 -*-
"""研发资本化（spec 2026-11-24-rd-capitalization）。不联网。

Damodaran：研发是投资，费用化让 ROIC 失真。A 股按申万行业年限把费用化研发资本化；投入资本补上
无形资产与开发支出（公司自行资本化的研发在这里）；NOPAT = EBIT×(1−t) + 研发 − 摊销（保留全额抵扣的税盾）。"""
import os
import pickle
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ashare_calibrate as C

Y = 1e8
P = "20251231"
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "cal_600183.pkl")


# ── Task 1：校准 ──────────────────────────────────────────────

def test_capitalize_rd_与美股同式():
    r = C.capitalize_rd([100, 120, 150, 180, 200, 240], 5)
    assert r["asset"] == pytest.approx(240 + 200 * 4 / 5 + 180 * 3 / 5 + 150 * 2 / 5 + 120 * 1 / 5)
    assert r["amort"] == pytest.approx((200 + 180 + 150 + 120 + 100) / 5)
    assert r["complete"] is True and r["covered"] == 6 and r["life"] == 5


def test_capitalize_rd_历史不足标不完整():
    r = C.capitalize_rd([50, 60, 70], 10)
    assert r["complete"] is False and r["covered"] == 3 and r["asset"] > 0


def test_capitalize_rd_全零或空():
    assert C.capitalize_rd([], 5)["asset"] == 0
    assert C.capitalize_rd([0, 0, 0], 5)["amort"] == 0
    assert C.capitalize_rd([None, 10.0], 5)["covered"] == 1          # None 跳过


def test_rd_history_只取年报且跳过缺失():
    inc = pd.DataFrame({"研发费用": [None, 1.5 * Y, 3.2 * Y], "营业总收入": [10 * Y] * 3},
                       index=["20231231", "20240630", "20241231"])
    assert C.rd_history(inc, "20241231") == [pytest.approx(3.2)]


def _bal(**extra):
    base = {"货币资金": 50.0, "交易性金融资产": 0.0, "应收票据及应收账款": 10.0, "存货": 20.0,
            "流动资产合计": 90.0, "固定资产净额": 40.0, "在建工程": 5.0, "非流动资产合计": 70.0,
            "短期借款": 0.0, "应付票据及应付账款": 10.0, "一年内到期的非流动负债": 0.0, "流动负债合计": 30.0,
            "长期借款": 0.0, "负债合计": 35.0, "少数股东权益": 0.0, "资产总计": 160.0}
    base.update(extra)
    return pd.DataFrame({k: [v * Y] for k, v in base.items()}, index=[P])


def test_opening_balances_无形部分():
    o = C.opening_balances(_bal(无形资产=12.0, 开发支出=3.0), P)
    assert o["intan"] == pytest.approx(15.0)
    assert C.opening_balances(_bal(), P)["intan"] == 0          # 两项都缺


def test_calibrate_写入rdcap与开关(monkeypatch):
    from tests.test_asof_quote import _offline, AS_OF
    if not os.path.exists(FIX):
        pytest.skip("缺离线夹具")
    cal_dict = pickle.load(open(FIX, "rb"))
    _offline(monkeypatch, cal_dict, {})
    cal = C.calibrate("600183", 10, stmts=cal_dict["stmts"], as_of=AS_OF, rd_life=10,
                      rd_disclosure={"total": 82.28e8, "capitalized": 16.45e8, "rate": 0.1999})
    assert cal["dcf"]["rd_cap"] == 1 and cal["dcf"]["rd_life"] == 10
    rc = cal["rdcap"]
    assert rc["life"] == 10 and rc["rd_hist"] and rc["asset"] > 0
    assert rc["intan0"] == pytest.approx(cal["open"]["intan"])
    assert rc["nonintan0"] == pytest.approx(cal["open"]["onca"] - cal["open"]["intan"])
    assert rc["rate"] == pytest.approx(0.1999) and rc["capitalized"] == pytest.approx(16.45)


def test_calibrate_缺省年限5_资本化率由金额推出(monkeypatch):
    from tests.test_asof_quote import _offline, AS_OF
    if not os.path.exists(FIX):
        pytest.skip("缺离线夹具")
    cal_dict = pickle.load(open(FIX, "rb"))
    _offline(monkeypatch, cal_dict, {})
    cal = C.calibrate("600183", 10, stmts=cal_dict["stmts"], as_of=AS_OF,
                      rd_disclosure={"total": 10e8, "capitalized": 2e8, "rate": None})
    assert cal["dcf"]["rd_life"] == 5 and cal["rdcap"]["rate"] == pytest.approx(0.2)
    cal2 = C.calibrate("600183", 10, stmts=cal_dict["stmts"], as_of=AS_OF)
    assert cal2["rdcap"]["rate"] is None and cal2["rdcap"]["total"] is None
