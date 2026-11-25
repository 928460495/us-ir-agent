"""账本侧短期投资：与 us-dcf 同一套主表勾稽（NVDA 2026-11-25 实跑）。不联网。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from ir_agent.sources.edgar_facts import MAPPED_TAGS, parse_companyfacts, slim

E, K, Q = "2026-01-25", ("2026-02-25", "10-K"), ("2026-05-20", "10-Q")


def _doc():
    def rows(v, *filings):
        return {"units": {"USD": [{"end": E, "val": v * 10**6, "filed": f, "form": form, "fy": 2026, "fp": "FY"}
                                  for f, form in filings]}}
    g = {t: rows(v, K, Q) for t, v in (
        ("AssetsCurrent", 125605), ("CashAndCashEquivalentsAtCarryingValue", 10605),
        ("AccountsReceivableNetCurrent", 38466), ("InventoryNet", 21403),
        ("PrepaidExpenseAndOtherAssetsCurrent", 3180),
        ("AvailableForSaleSecuritiesDebtMaturitiesWithinOneYearFairValue", 20430))}
    g["DebtSecuritiesCurrent"], g["EquitySecuritiesFvNi"] = rows(39065, Q), rows(12886, Q)
    return {"facts": {"us-gaap": g, "dei": {}}}


def _sti(doc):
    return [(f.value, f.as_of) for f in parse_companyfacts(doc, "0125", "s") if f.key == "trading_finasset"]


def test_账本取勾稽值():
    assert _sti(_doc()) == [(Decimal(51951) * 10**6, date(2026, 5, 20))]


def test_裁剪后勾稽所需科目仍在():
    assert {"DebtSecuritiesCurrent", "EquitySecuritiesFvNi", "PrepaidExpenseAndOtherAssetsCurrent"} <= MAPPED_TAGS
    assert _sti(slim(_doc())) == _sti(_doc())


def test_M2_不产生合成科目的补缺说明():
    notes = []
    d = _doc()
    d["facts"]["us-gaap"]["ShortTermInvestments"] = {"units": {"USD": [
        {"end": "2025-01-26", "val": 5, "filed": "2025-02-26", "form": "10-K", "fy": 2025, "fp": "FY"}]}}
    facts = parse_companyfacts(d, "0125", "s", notes=notes)
    assert not any("_CurrentInvestments" in n for n in notes)
    assert {f.period: f.value for f in facts if f.key == "trading_finasset"} == {
        "2026FY": Decimal(51951) * 10**6, "2025FY": Decimal(5)}
