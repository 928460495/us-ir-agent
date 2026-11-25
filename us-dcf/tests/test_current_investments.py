"""短期投资按资产负债表主表勾稽取数（NVDA 2026 财年起换标签，2026-11-25 实跑）。不联网。"""
from __future__ import annotations

import pytest

import current_investments as CI
import us_data as D


def _n(end, val, filed, form="10-Q"):
    return {"end": end, "val": val * 1_000_000, "filed": filed, "form": form}


def _g(**tags):
    return {t: {"units": {"USD": rows}} for t, rows in tags.items()}


E = "2026-01-25"
K, Q = ("2026-02-25", "10-K"), ("2026-05-20", "10-Q")


def _nvda():
    both = lambda v: [_n(E, v, *K), _n(E, v, *Q)]
    return _g(AssetsCurrent=both(125605), CashAndCashEquivalentsAtCarryingValue=both(10605),
              AccountsReceivableNetCurrent=both(38466), InventoryNet=both(21403),
              PrepaidExpenseAndOtherAssetsCurrent=both(3180),
              AvailableForSaleSecuritiesDebtMaturitiesWithinOneYearFairValue=both(20430),
              DebtSecuritiesCurrent=[_n(E, 39065, *Q)], EquitySecuritiesFvNi=[_n(E, 12886, *Q)])


def _tied(g, end=E):
    rows = CI.add_tied(g).get(CI.TAG, {}).get("units", {}).get("USD", [])
    return [(r["val"] / 1e6, r["filed"], r["form"]) for r in rows if r["end"] == end]


def test_债券加有价股票与主表勾稽上():
    assert _tied(_nvda()) == [(51951.0, "2026-05-20", "10-K")]      # 财年末期的值按 10-K 期标，时点取补全那天


def test_单一主表科目勾稽上时取它_不叠加附注股票():
    g = _nvda()
    g["MarketableSecuritiesCurrent"] = {"units": {"USD": [_n(E, 51951, *K)]}}
    assert _tied(g)[0][:2] == (51951.0, "2026-02-25")


def test_勾稽不上就不给_交回原有优先序():
    g = _nvda()
    g["AssetsCurrent"] = {"units": {"USD": [_n(E, 160000, *K)]}}     # 另有未覆盖的大科目（如非贸易应收）
    assert _tied(g) == []
    assert CI.add_tied(g) is g


def test_没有流动资产或现金时不给():
    g = _nvda(); del g["AssetsCurrent"]
    assert _tied(g) == []
    g = _nvda(); del g["CashAndCashEquivalentsAtCarryingValue"]
    assert _tied(g) == []


def test_附注口径恰好小于残差也不算勾稽():
    g = _nvda(); del g["DebtSecuritiesCurrent"]
    assert _tied(g) == []


def test_us_data短期投资优先取勾稽值():
    assert D.TAGS_BS["短期投资"][0] == CI.TAG
    diag = {"缺失": []}
    got = D.pick_series(CI.add_tied(_nvda()), D.TAGS_BS["短期投资"], [E], False, D.MN, None, diag, "短期投资")
    assert got == [pytest.approx(51951.0)]
