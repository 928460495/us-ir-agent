"""XBRL companyfacts → 账本事实。

每条 XBRL 条目带 `start` / `end` / `val` / `form` / `filed`。规则:
  - 期间由起止日期 + 财年末推出（`us_periods.fiscal_label`），不用条目的 `fy` / `fp`；
  - `as_of = filed`。同一期数据在后续申报里作为比较期再出现、或被 10-K/A 修订，
    都按各自的申报日作为新版本入账 —— 时点守卫因此能还原「当时看到的是哪个数」；
  - 只收定期报告（10-K / 10-Q 及其修订）。

**标签选择**（思路来自 us-dcf `pick_series`）: 一个账本键有多条候选标签时，在覆盖最新期间的
候选里按优先序选主标签，主标签缺期才从次优标签补，每次补缺记一条说明。逐期混取会静默灌入语义不同的数 ——
us-dcf 实测苹果「税前利润」的第二候选标签装的是境外税前利润。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ir_agent.ledger import Fact, Method
from ir_agent.periods import sort_key
from ir_agent.sources import edgar
from ir_agent.sources.us_periods import fiscal_label

FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A")
MONEY_UNIT, CURRENCY, SHARE_UNIT = "美元", "USD", "股"

# 账本键 → 候选标签（按优先序）。来自 us-dcf TAGS_IS / TAGS_BS / TAGS_CF / TAGS_SHARES。
_DURATION: dict[str, tuple[str, ...]] = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"),
    "cost_of_revenue": ("CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold",
                        "CostOfServices"),
    # 公司自报的毛利：勾稽「毛利 = 营业收入 − 营业成本」用它做独立核对
    "gross_profit": ("GrossProfit",),
    "rd_expense": ("ResearchAndDevelopmentExpense",
                   "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
    "sga_expense": ("SellingGeneralAndAdministrativeExpense",),
    "selling_expense": ("SellingAndMarketingExpense",),
    "admin_expense": ("GeneralAndAdministrativeExpense",),
    "operating_profit": ("OperatingIncomeLoss",),
    # 不收 InterestIncomeExpenseNet：那是净额，利息收入为正，与「利息支出」符号相反
    "interest_expense_fe": ("InterestExpense", "InterestExpenseDebt"),
    "total_profit": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
    "income_tax": ("IncomeTaxExpenseBenefit",),
    "net_profit": ("ProfitLoss", "NetIncomeLoss"),
    "minority_interest_profit": ("NetIncomeLossAttributableToNoncontrollingInterest",),
    # CAT 的 10-K 不报 NetIncomeLoss，只报「普通股股东可得净利润」（无优先股时与归母相等）
    "net_profit_attr_parent": ("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"),
    "cf_operating": ("NetCashProvidedByUsedInOperatingActivities",
                     "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
              "PaymentsForCapitalImprovements"),
    "sbc": ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"),
    "buyback": ("PaymentsForRepurchaseOfCommonStock",),
    "dividends_paid": ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends"),
}
_INSTANT: dict[str, tuple[str, ...]] = {
    "cash_and_equivalents": ("CashAndCashEquivalentsAtCarryingValue",
                             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "trading_finasset": ("ShortTermInvestments", "MarketableSecuritiesCurrent",
                         "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                         "AvailableForSaleSecuritiesDebtMaturitiesWithinOneYearFairValue"),
    "accounts_receivable": ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent"),
    "inventory": ("InventoryNet",),
    "current_assets": ("AssetsCurrent",),
    "fixed_asset": ("PropertyPlantAndEquipmentNet",
                    "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
                    "PropertyPlantAndEquipmentNetExcludingFinanceLeaseRightOfUseAsset"),
    "useright_asset": ("OperatingLeaseRightOfUseAsset",),
    "long_equity_invest": ("EquityMethodInvestments",),
    "goodwill": ("Goodwill",),
    "intangible_asset": ("FiniteLivedIntangibleAssetsNet", "IntangibleAssetsNetExcludingGoodwill"),
    "total_assets": ("Assets",),
    "accounts_payable": ("AccountsPayableCurrent", "AccountsPayableAndAccruedLiabilitiesCurrent"),
    "short_loan": ("ShortTermBorrowings", "CommercialPaper", "OtherShortTermBorrowings"),
    "noncurrent_liab_1y": ("LongTermDebtCurrent", "DebtCurrent"),
    "current_liabilities": ("LiabilitiesCurrent",),
    # 不收 LongTermDebt：它含一年内到期部分，与 noncurrent_liab_1y 叠加会重复计入有息负债
    "long_loan": ("LongTermDebtNoncurrent",),
    "lease_liability": ("OperatingLeaseLiabilityNoncurrent",),
    "total_liabilities": ("Liabilities",),
    "retained_earnings": ("RetainedEarningsAccumulatedDeficit",),
    "equity_attr_parent": ("StockholdersEquity",),
    "minority_equity": ("MinorityInterest",),
    "total_equity": ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                     "StockholdersEquity"),
}
_SHARES_DURATION: dict[str, tuple[str, ...]] = {
    "diluted_shares": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "basic_shares": ("WeightedAverageNumberOfSharesOutstandingBasic",
                     "WeightedAverageNumberOfSharesOutstanding"),
}
# 折旧摊销: 有合计就只记合计（记在 depreciation_fixed_asset），否则记分项 ——
# aggregates.DA_PARTS 会把这几个键加总，两种都记就重复。MSFT / GOOGL 只报分项（us-dcf 实测）。
_DA_TOTAL = ("DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
             "DepreciationAndAmortization")
_DA_PARTS: dict[str, tuple[str, ...]] = {
    "depreciation_fixed_asset": ("Depreciation",),
    "amortization_intangible": ("AmortizationOfIntangibleAssets",),
    "amortization_useright": ("FinanceLeaseRightOfUseAssetAmortization",),
}
_FP_LABEL = {"FY": "FY", "Q1": "Q1", "Q2": "H1", "Q3": "Q1-Q3"}

# (期间, 申报日, 值, 标签)
_Row = tuple[str, date, Decimal, str]


def _rows(gaap: dict, tag: str, unit: str, duration: bool, fye: str) -> list[_Row]:
    out = []
    for x in gaap.get(tag, {}).get("units", {}).get(unit, []):
        if x.get("form") not in FORMS or not x.get("filed"):
            continue
        has_start = bool(x.get("start"))
        if has_start != duration:
            continue
        start = date.fromisoformat(x["start"]) if has_start else None
        period = fiscal_label(start, date.fromisoformat(x["end"]), fye)
        if period is None:
            continue
        out.append((period, date.fromisoformat(x["filed"]), Decimal(str(x["val"])), tag))
    return out


def _pick(gaap: dict, key: str, tags: tuple[str, ...], unit: str, duration: bool, fye: str,
          notes: list[str], skip: set[str] = frozenset()) -> list[_Row]:
    """按覆盖度选主标签；主标签缺的期间按优先序从其他标签补，并记说明。"""
    cands = {t: [r for r in _rows(gaap, t, unit, duration, fye) if r[0] not in skip] for t in tags}
    cands = {t: rs for t, rs in cands.items() if rs}
    if not cands:
        return []
    cover = {t: {r[0] for r in rs} for t, rs in cands.items()}
    # 主标签: 覆盖最新期间的候选里按优先序取。不按全历史覆盖度 —— 那会让一个历史
    # 更长、语义不同的次优标签整体顶替主标签（审查实测: NetIncomeLoss 七年历史
    # 压过只有两年的 ProfitLoss，全口径净利润静默变成归母口径）。
    latest = max(set().union(*cover.values()), key=sort_key)
    current = [t for t in tags if t in cover and latest in cover[t]]
    best = current[0]
    if best != tags[0] and tags[0] in cands:
        notes.append(f"{key} 主标签取 {best}（首选 {tags[0]} 不覆盖最新期 {latest}）")
    elif best != tags[0]:
        notes.append(f"{key} 主标签取 {best}（首选 {tags[0]} 无数据）")
    out = list(cands[best])
    for p in sorted(set().union(*cover.values()) - cover[best]):
        t = next(t for t in tags if t in cover and p in cover[t])
        out.extend(r for r in cands[t] if r[0] == p)
        notes.append(f"{key}@{p} 取自 {t}（主标签 {best} 无此期）")
    return out


def _facts(key: str, rows: list[_Row], source_id: str, money: bool) -> list[Fact]:
    """同一期按申报日排序，只在数值变化时记新版本。后续申报把比较期原值再报一遍不是新信息 ——
    记成新版本会让依赖它的计算值带上晚得多的披露日（联网验收：苹果 ROE 显示 2026-07-31）。"""
    last: dict[str, Decimal] = {}
    out = []
    for period, filed, val, _tag in sorted(rows, key=lambda r: (r[0], r[1])):
        if last.get(period) == val:
            continue
        last[period] = val
        out.append(Fact(key=key, value=val, period=period, as_of=filed, source_id=source_id,
                        method=Method.REPORTED,
                        unit=MONEY_UNIT if money else SHARE_UNIT,
                        currency=CURRENCY if money else None))
    return out


def _cover_page_shares(dei: dict, source_id: str) -> list[Fact]:
    """封面在外股数。截止日是封面日期而非季末，按日期推不出期间；封面数不带比较期，
    条目的 fy / fp 就是本申报自己的期间，这里例外地用它标期。"""
    rows = []
    for x in dei.get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", []):
        if x.get("form") not in FORMS or x.get("fp") not in _FP_LABEL or not x.get("fy"):
            continue
        rows.append((f"{x['fy']}{_FP_LABEL[x['fp']]}", date.fromisoformat(x["filed"]),
                     Decimal(str(x["val"])), "EntityCommonStockSharesOutstanding"))
    return _facts("shares_outstanding", rows, source_id, money=False)


def parse_companyfacts(doc: dict, fye_mmdd: str, source_id: str,
                       notes: list[str] | None = None) -> list[Fact]:
    notes = notes if notes is not None else []
    gaap = doc.get("facts", {}).get("us-gaap", {})
    out: list[Fact] = []
    for table, duration in ((_DURATION, True), (_INSTANT, False)):
        for key, tags in table.items():
            out += _facts(key, _pick(gaap, key, tags, "USD", duration, fye_mmdd, notes),
                          source_id, money=True)
    for key, tags in _SHARES_DURATION.items():
        out += _facts(key, _pick(gaap, key, tags, "shares", True, fye_mmdd, notes),
                      source_id, money=False)

    total = _pick(gaap, "depreciation_fixed_asset", _DA_TOTAL, "USD", True, fye_mmdd, notes)
    out += _facts("depreciation_fixed_asset", total, source_id, money=True)
    has_total = {r[0] for r in total}
    for key, tags in _DA_PARTS.items():
        out += _facts(key, _pick(gaap, key, tags, "USD", True, fye_mmdd, notes, skip=has_total),
                      source_id, money=True)

    out += _cover_page_shares(doc.get("facts", {}).get("dei", {}), source_id)
    return out + _revenue_total(out)


def _revenue_total(facts: list[Fact]) -> list[Fact]:
    """US GAAP 没有「营业总收入 / 营业收入」两层，营业总收入即营业收入。按 A 股
    `_alias_facts` 同一做法补出（COMPUTED + derived_from），否则以 revenue_total 为
    分母的指标（Altman Z 等）会误报为「数据缺失」。"""
    return [Fact(key="revenue_total", value=f.value, unit=f.unit, currency=f.currency,
                 period=f.period, as_of=f.as_of, source_id=f.source_id, method=Method.COMPUTED,
                 derived_from=(f"revenue@{f.period}",))
            for f in facts if f.key == "revenue"]


class NoUsGaapFacts(Exception):
    """公司没有 us-gaap 事实（多为只报 IFRS 的外国私人发行人）。"""


def load_us_statements(ticker: str, store, notes: list[str] | None = None
                       ) -> tuple[list[Fact], str, dict[str, str]]:
    """EDGAR 取数的唯一入口：返回 (facts, 财年末 MMDD, {接口: source_id})。

    解析不出美元事实就报错 —— 不能让调用方把 FMP 降级成取值主源（FMP 只用于核对）。
    """
    info = edgar.resolve_ticker(ticker, store)
    sub, sub_sid = edgar.fetch_submissions(info.cik, store)
    fye = sub.get("fiscalYearEnd") or ""
    if len(fye) != 4:
        raise ValueError(f"{ticker} 的 submissions 没有财年末（fiscalYearEnd={fye!r}）。")
    doc, cf_sid = edgar.fetch_companyfacts(info.cik, store)
    if not doc.get("facts", {}).get("us-gaap"):
        raise NoUsGaapFacts(f"{ticker}（CIK {info.cik}）没有 us-gaap 事实，可能只报 IFRS（20-F / 40-F）。")
    facts = parse_companyfacts(doc, fye, cf_sid, notes=notes)
    if not any(f.unit == MONEY_UNIT for f in facts):
        raise NoUsGaapFacts(f"{ticker}（CIK {info.cik}）的 us-gaap 事实里没有以美元计的定期报告数据。")
    return facts, fye, {"submissions": sub_sid, "companyfacts": cf_sid}


def fetch_statements_us(ticker: str, store, notes: list[str] | None = None
                        ) -> tuple[list[Fact], dict[str, str]]:
    """返回 (facts, {接口: source_id})，与 eastmoney.fetch_statements_em 同形状。"""
    facts, _fye, sids = load_us_statements(ticker, store, notes)
    return facts, sids
