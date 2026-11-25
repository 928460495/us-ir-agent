"""短期投资按资产负债表主表勾稽取数。不依赖其他模块，账本层（ir_agent）与估值层共用。

**为什么不能只排标签优先序。** 公司会换标签：英伟达 FY2026 年报把「有价证券」改用自定义标签
（companyfacts 不收），次年 10-Q 又拆成「债券」「有价股票」两行。按优先序往下找，落到的是附注口径
「一年内到期的可供出售债券」204 亿，主表是 519.5 亿；而三表照样平 —— 差额被倒挤进「其他流动资产」。

**做法**：流动资产合计 − 现金 − 应收 − 存货 − 申报的其他流动资产 = 残差；哪个（组）主表科目与残差对得上就是它。
「有价股票」在别的公司可能是附注合计，所以只在加上它才对得上时才加。对不上（另有未列科目，如非贸易应收）
不给值，交回原有的优先序。
"""

from __future__ import annotations

TAG = "_CurrentInvestmentsTied"
TOL = 0.0005                       # 对得上的容差：占流动资产合计
FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A")

_AC = "AssetsCurrent"
_CASH = "CashAndCashEquivalentsAtCarryingValue"
_AR = ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent")
_INV = ("InventoryNet",)
_PREPAID_OTHER = "PrepaidExpenseAndOtherAssetsCurrent"
_OTHER = "OtherAssetsCurrent"
_PREPAID = "PrepaidExpenseCurrent"
# 主表科目（流动部分）。附注口径（…MaturitiesWithinOneYearFairValue）不在此列
_FACE = ("ShortTermInvestments", "MarketableSecuritiesCurrent",
         "AvailableForSaleSecuritiesDebtSecuritiesCurrent", "DebtSecuritiesCurrent")
_EQUITY = ("EquitySecuritiesFvNiCurrent", "EquitySecuritiesFvNi")

COMPONENTS = frozenset({_AC, _CASH, _PREPAID_OTHER, _OTHER, _PREPAID, *_AR, *_INV, *_FACE, *_EQUITY})


def _combos() -> list[tuple[str, ...]]:
    """候选由简到繁：单个主表科目优先，再试「主表科目 + 有价股票」，最后只有有价股票。"""
    return ([(t,) for t in _FACE] + [(t, e) for t in _FACE for e in _EQUITY] + [(e,) for e in _EQUITY])


def _tied(v: dict[str, float]) -> float | None:
    ac, cash = v.get(_AC), v.get(_CASH)
    if not ac or cash is None:
        return None
    first = lambda tags: next((v[t] for t in tags if t in v), 0)
    base = ac - cash - first(_AR) - first(_INV)
    others = []                                  # 「其他流动资产」的申报写法，逐种试
    if _PREPAID_OTHER in v:
        others.append(v[_PREPAID_OTHER])
    if _OTHER in v:
        others.append(v[_OTHER] + v.get(_PREPAID, 0))
        others.append(v[_OTHER])
    if not others:
        return None                              # 没有申报其他流动资产，残差里混着它，无从勾稽
    for other in others:
        left = base - other
        for combo in _combos():
            if all(t in v for t in combo):
                s = sum(v[t] for t in combo)
                if s > 0 and abs(s - left) <= TOL * ac:
                    return s
    return None


def add_tied(gaap: dict) -> dict:
    """返回带 `TAG` 合成科目的 us-gaap 字典；一期都勾稽不上就原样返回（同一对象）。

    按申报日还原版本：每个期末逐个申报日看当时已披露的值，首次对得上（或对上的值变了）记一条，
    申报日取那一天 —— 账本的时点守卫照常生效。财年末的期标成 10-K（补全的分项可能来自次年 10-Q 的比较栏）。
    """
    seen: dict[str, list[tuple[str, str, float, str]]] = {}          # 期末 → [(申报日, 标签, 值, 表格)]
    for t in COMPONENTS & set(gaap):
        for x in gaap[t].get("units", {}).get("USD", []):
            if x.get("start") or x.get("form") not in FORMS or not x.get("filed"):
                continue
            seen.setdefault(x["end"], []).append((x["filed"], t, x["val"], x["form"]))
    out = []
    for end, rows in seen.items():
        forms = {f for _, t, _, f in rows if t == _AC}
        form = "10-K" if any(f.startswith("10-K") for f in forms) else "10-Q"
        rows.sort(key=lambda r: r[0])
        now: dict[str, float] = {}
        last = None
        for filed in sorted({r[0] for r in rows}):
            now.update({t: v for d, t, v, _ in rows if d == filed})
            s = _tied(now)
            if s is not None and s != last:
                last = s
                out.append({"end": end, "val": s, "filed": filed, "form": form})
    return {**gaap, TAG: {"units": {"USD": out}}} if out else gaap
