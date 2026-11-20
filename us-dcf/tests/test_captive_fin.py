"""美股自营金融应收款与受限现金（spec 2026-11-20-nonop-financial-assets，Task 6）。不联网。

福特 2025：NotesAndLoansReceivableNetCurrent 491 亿 + Noncurrent 614 亿美元，占资产 38%。此前被倒挤进「其他流动 /
非流动资产」，随营收增长扣自由现金流；而 Ford Credit 的融资债务已计入净负债 —— 双重低估。
GM、DE 用公司自定义标签，companyfacts 取不到：只能按「购买金融应收款」的现金流提示，不猜数。"""
from __future__ import annotations

import pandas as pd
import pytest

import us_data as D

ENDS = ["2024-12-31", "2025-12-31"]


def _inst(tag_vals: dict) -> dict:
    g = {}
    for tag, vals in tag_vals.items():
        g[tag] = {"units": {"USD": [dict(end=e, val=v * 1e6, form="10-K", filed=e) for e, v in zip(ENDS, vals)]}}
    return g


def _dur(g: dict, tag: str, vals) -> dict:
    g[tag] = {"units": {"USD": [dict(start=f"{e[:4]}-01-01", end=e, val=v * 1e6, form="10-K", filed=e)
                                for e, v in zip(ENDS, vals)]}}
    return g


def _bal(**over):
    b = pd.DataFrame({"现金及等价物": [25000.0, 28000.0], "短期投资": [17000.0, 18000.0],
                      "其他流动资产": [60000.0, 65000.0], "其他非流动资产": [70000.0, 75000.0],
                      "资产总计": [285000.0, 289160.0]}, index=ENDS)
    for k, v in over.items():
        b[k] = v
    return b


def _inc(rev=(176000.0, 185000.0)):
    return pd.DataFrame({"营业收入": list(rev)}, index=ENDS)


def test_福特式自营金融_识别并提示_不改估值():
    """终审 I1/I2（2026-11-20 裁定）：福特金融子公司的收入与融资成本在经营利润里、融资债务在净负债里；
    把应收款按账面并入短期投资会与 EBIT 里的金融利润重复计算，还会额外计现金收益。正确处理要分部数据，
    companyfacts 做不到 —— 只识别、提示，不改报表。"""
    g = _inst({"NotesAndLoansReceivableNetCurrent": [47000, 49130],
               "NotesAndLoansReceivableNetNoncurrent": [59000, 61449]})
    bal, diag = _bal(), {}
    before = bal.copy()
    D._captive_fin(g, bal, _inc(), ENDS, diag)
    pd.testing.assert_frame_equal(bal, before)
    w = " ".join(diag["警告"])
    assert "自营金融" in w and "110,579" in w and "分部" in w and "人工核对" in w


def test_低于阈值视为经营性_不动():
    g = _inst({"NotesAndLoansReceivableNetCurrent": [1000, 1200], "NotesAndLoansReceivableNetNoncurrent": [500, 600]})
    bal, diag = _bal(), {}
    before = bal.copy()
    D._captive_fin(g, bal, _inc(), ENDS, diag)
    pd.testing.assert_frame_equal(bal, before)
    assert "非经营金融资产" not in diag


def test_自定义标签取不到_按购买金融应收款提示人工核对():
    g = _dur({}, "PaymentsToAcquireFinanceReceivables", [25000, 26340])
    bal, diag = _bal(), {}
    before = bal.copy()
    D._captive_fin(g, bal, _inc(rev=(50000.0, 45000.0)), ENDS, diag)
    pd.testing.assert_frame_equal(bal, before)
    assert any("疑似" in x and "自定义标签" in x and "人工核对" in x for x in diag["警告"])


def test_没有任何相关标签的公司完全不变():
    bal, diag = _bal(), {}
    before = bal.copy()
    D._captive_fin({}, bal, _inc(), ENDS, diag)
    D._restricted_cash({}, bal, ENDS, diag)
    pd.testing.assert_frame_equal(bal, before)
    assert diag == {}


def test_现金退用含受限口径时扣除受限现金():
    g = _inst({"CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": [8000, 8533],
               "RestrictedCashAndCashEquivalents": [250, 257]})
    bal, diag = _bal(**{"现金及等价物": [8000.0, 8533.0]}), {}
    D._restricted_cash(g, bal, ENDS, diag)
    assert bal.at["2025-12-31", "现金及等价物"] == pytest.approx(8533 - 257)
    assert any("受限现金" in x for x in diag["推导"])


def test_现金用不含受限的标签时不重复扣():
    g = _inst({"CashAndCashEquivalentsAtCarryingValue": [8000, 8276],
               "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": [8250, 8533],
               "RestrictedCashAndCashEquivalents": [250, 257]})
    bal, diag = _bal(**{"现金及等价物": [8000.0, 8276.0]}), {}
    D._restricted_cash(g, bal, ENDS, diag)
    assert bal.at["2025-12-31", "现金及等价物"] == pytest.approx(8276)
