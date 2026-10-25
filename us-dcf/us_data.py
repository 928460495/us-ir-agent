# -*- coding: utf-8 -*-
"""美股 DCF 模板 — 数据层。SEC EDGAR XBRL 为主库，FMP 交叉校验，Stooq 行情，FRED 无风险利率。

只负责取数与归一化，不含业务判断。单位统一为【百万美元】。

设计要点（都是实测踩出来的坑，不是假想）：
  1. 同一概念在不同年份可能换标签 —— NVDA 营收 FY2017–FY2022 用
     RevenueFromContractWithCustomerExcludingAssessedTax，之后改用 Revenues。
     「取第一个存在的标签」会拿到一条 4 年前就停更的序列。→ 逐期末日按标签优先序合并。
  2. 同一期末在不同申报里可能有多个值（重述）。→ 取【申报日最新】的那个。
  3. companyfacts 不做拆股回溯：NVDA FY2022 摊薄股数在三次申报里都是 2,535M（拆股前），
     而 FY2023 是 25,070M（拆股后）。→ 按【该事实的申报日之后发生的拆股】累乘还原。
  4. 标签缺失因公司而异（KO 不单列 R&D、CAT 不用 StockholdersEquity），缺项必须优雅降级，
     不能崩，也不能静默填 0 —— 静默填 0 正是最危险的失败模式。
"""
from __future__ import annotations
import json, os, time, urllib.parse, urllib.request, urllib.error, datetime as dt, tempfile
import numpy as np, pandas as pd

MN = 1e6                       # 百万美元
_GENERIC_UA = {"User-Agent": "Mozilla/5.0"}
_CACHE = os.path.join(tempfile.gettempdir(), "us_dcf_cache")


class DataError(RuntimeError): pass


# ============================================================
# HTTP
# ============================================================
def sec_headers() -> dict[str, str]:
    """SEC 要求请求头里写明身份与联系方式。只从环境变量取，代码里不留任何邮箱。"""
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "未设置 SEC_USER_AGENT。SEC 要求请求头写明身份与联系邮箱，例如：\n"
            '  export SEC_USER_AGENT="Your Name your@email"')
    return {"User-Agent": ua}


def _default_headers(url: str) -> dict[str, str]:
    """只有 sec.gov 带 SEC 身份；其他站点用中性请求头，不把联系方式发给无关服务。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    return sec_headers() if host == "sec.gov" or host.endswith(".sec.gov") else _GENERIC_UA


def _get(url: str, hdr: dict | None = None, timeout: int = 30, retries: int = 3) -> bytes:
    headers = hdr or _default_headers(url)       # 缺 SEC 身份在这里就报错，不进重试
    last = None
    for i in range(retries):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=timeout).read()
        except Exception as e:
            last = e
            time.sleep(0.4 * (i + 1))
    raise DataError(f"请求失败（重试{retries}次）：{url[:80]} → {type(last).__name__}: {str(last)[:120]}")


def _json(url: str, hdr: dict | None = None, **kw):
    return json.loads(_get(url, hdr, **kw))


def _cached(name: str, fn, ttl_hours: int = 12):
    """companyfacts 有 3.8MB / 7 秒，反复调试时必须缓存。"""
    os.makedirs(_CACHE, exist_ok=True)
    p = os.path.join(_CACHE, name)
    if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < ttl_hours * 3600:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    d = fn()
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f)
    return d


# ============================================================
# 代码解析 / 公司识别
# ============================================================
def resolve(symbol: str) -> dict:
    """ticker → CIK、公司名、SIC 行业、财年末。"""
    sym = str(symbol).strip().upper()
    if not sym or not all(c.isalnum() or c in ".-" for c in sym):
        raise DataError(f"「{symbol}」不是合法的美股代码。")
    mp = _cached("tickers.json", lambda: _json("https://www.sec.gov/files/company_tickers.json"), 72)
    hit = next((v for v in mp.values() if v["ticker"].upper() == sym), None)
    if hit is None:
        raise DataError(f"在 SEC 的 ticker→CIK 映射里找不到「{sym}」。\n"
                        f"本模板只支持在 SEC 申报的美股（ADR 多数可用；仅在境外上市的不行）。")
    cik = f"{hit['cik_str']:010d}"
    sub = _cached(f"sub_{cik}.json", lambda: _json(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    return dict(cik=cik, ticker=sym, name=sub.get("name") or hit["title"],
                sic=str(sub.get("sic") or ""), sic_desc=sub.get("sicDescription") or "",
                fye=sub.get("fiscalYearEnd") or "", exchange=(sub.get("exchanges") or [None])[0])


def assert_non_financial(meta: dict) -> None:
    """按 SIC 判定，比 A 股版的科目关键词法干净得多。
    6000–6499 银行/保险/券商 → FCFF 不可定义；6500–6799 地产/信托/投资另有专用模型。"""
    sic = meta.get("sic") or ""
    if not sic.isdigit():
        return
    n = int(sic)
    if 6000 <= n <= 6499:
        raise DataError(
            f"SIC {sic}（{meta['sic_desc']}）属于银行/保险/券商。\n"
            f"金融机构的自由现金流不可定义，FCFF-DCF 不适用 —— 请改用股利折现（DDM）、"
            f"超额收益（Residual Income）或内含价值模型。")
    if 6500 <= n <= 6799:
        raise DataError(
            f"SIC {sic}（{meta['sic_desc']}）属于地产/信托/投资类。\n"
            f"REIT 应以 FFO/AFFO 与 NAV 估值，投资控股公司应看穿至底层资产，本模板不适用。")


# ============================================================
# XBRL 事实抽取
# ============================================================
def facts(cik: str) -> dict:
    d = _cached(f"cf_{cik}.json", lambda: _json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"))
    g = d.get("facts", {}).get("us-gaap")
    if not g:
        raise DataError(f"CIK {cik} 没有 us-gaap 事实数据（可能是外国私人发行人，只报 IFRS）。")
    return g


def _months(a: str, b: str) -> int:
    return (int(b[:4]) * 12 + int(b[5:7])) - (int(a[:4]) * 12 + int(a[5:7]))


def _facts_by_end(g: dict, tags: list[str], duration: bool, forms=("10-K",)) -> dict:
    """返回 {期末日: (值, 申报日, 标签)}。
    优先序 = tags 的顺序；同一期末同一标签有多条时取【申报日最新】的（重述后的值）。"""
    out: dict[str, dict] = {}
    for t in tags:
        node = g.get(t)
        if not node:
            continue
        for unit, arr in node.get("units", {}).items():
            if unit not in ("USD", "shares", "USD/shares"):
                continue
            for x in arr:
                form = x.get("form") or ""
                if not any(form.startswith(f) for f in forms):
                    continue
                if duration:
                    if not x.get("start") or not (11 <= _months(x["start"], x["end"]) <= 13):
                        continue
                else:
                    if x.get("start"):
                        continue
                slot = out.setdefault(x["end"], {})
                prev = slot.get(t)
                if prev is None or x.get("filed", "") > prev[1]:
                    slot[t] = (x["val"], x.get("filed", ""), t)
    return {end: next(v[t] for t in tags if t in v) for end, v in out.items() if any(t in v for t in tags)}


def series(g: dict, tags: list[str], duration: bool = True, scale: float = MN,
           splits: list[tuple[str, float]] | None = None) -> dict:
    """跨标签合并的年度序列 {期末日: 值}。splits 给定时按申报日做拆股还原。"""
    raw = _facts_by_end(g, tags, duration)
    out = {}
    for end, (val, filed, _tag) in raw.items():
        f = 1.0
        if splits:
            # 该事实是「申报日当天的口径」；其后发生的拆股都要补乘上去
            for d, r in splits:
                if filed and d > filed:
                    f *= r
        out[end] = val * f / scale
    return dict(sorted(out.items()))


def pick_series(g: dict, tags: list[str], ends: list[str], duration: bool, scale: float,
                splits, diag: dict, name: str) -> list | None:
    """按【对目标期间的覆盖度】选定一个主标签，而不是逐期在多个标签之间混取。

    逐期混取有真实危险：AAPL「税前利润」的第二候选标签
    IncomeLoss...MinorityInterestAndIncomeLossFromEquityMethodInvestments
    实际是【境外】税前利润（末值 55,763 vs 真实 132,729）。主标签只要缺一期，
    就会被灌进一个语义完全不同的数字，而且不会报错。

    所以：先选覆盖最全的那个标签（并列时按优先序），只有它仍有缺口时才从
    次优标签补，并把每一次跨标签补缺都记进诊断，让使用者看得见。"""
    cands = {t: series(g, [t], duration, scale, splits) for t in tags if t in g}
    cands = {t: v for t, v in cands.items() if v}
    if not cands:
        diag["缺失"].append(name)
        return None
    best = max(cands, key=lambda t: (sum(1 for e in ends if e in cands[t]), -tags.index(t)))
    out = dict(cands[best])
    for e in [e for e in ends if e not in out]:
        for t in tags:
            if t != best and t in cands and e in cands[t]:
                out[e] = cands[t][e]
                diag.setdefault("跨标签补缺", []).append(f"{name} {e} 取自 {t}（主标签 {best} 无此期）")
                break
    got = sum(1 for e in ends if e in out)
    if got < len(ends):
        diag.setdefault("期间不全", []).append(f"{name} 仅 {got}/{len(ends)} 期有值（主标签 {best}）")
    return [out.get(e) for e in ends]


def split_history(ticker: str) -> list[tuple[str, float]]:
    """拆股事件 [(除权日, 比例)]。取不到就返回空 —— 不阻断流程，但会在报告里提示。"""
    try:
        import yfinance as yf
        s = yf.Ticker(ticker).splits
        return [(ts.strftime("%Y-%m-%d"), float(r)) for ts, r in s.items() if r and r != 1.0]
    except Exception:
        return []


# ============================================================
# 科目映射（US GAAP）
# ============================================================
# 每项给一条【优先序标签链】。顺序即优先级：越靠前越贴近想要的口径。
TAGS_IS = {
    "营业收入": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                 "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"],
    "营业成本": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold", "CostOfServices"],
    "研发费用": ["ResearchAndDevelopmentExpense",
                 "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"],
    "销售及管理费用": ["SellingGeneralAndAdministrativeExpense"],
    "销售费用": ["SellingAndMarketingExpense"],
    "管理费用": ["GeneralAndAdministrativeExpense"],
    "经营利润": ["OperatingIncomeLoss"],
    "利息支出": ["InterestExpense", "InterestExpenseDebt", "InterestIncomeExpenseNet"],
    "利息及投资收益": ["InvestmentIncomeInterest", "InterestAndDividendIncomeOperating"],
    "其他损益净额": ["OtherNonoperatingIncomeExpense", "NonoperatingIncomeExpense"],
    "税前利润": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                 "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "所得税": ["IncomeTaxExpenseBenefit"],
    "净利润": ["ProfitLoss", "NetIncomeLoss"],
    "少数股东损益": ["NetIncomeLossAttributableToNoncontrollingInterest"],
    "归母净利润": ["NetIncomeLoss"],
}
TAGS_BS = {   # 时点数
    # INTC 2021–2023 只申报含受限资金的口径（受限部分很小，可接受）
    "现金及等价物": ["CashAndCashEquivalentsAtCarryingValue",
                     "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "短期投资": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                 "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
                 "AvailableForSaleSecuritiesDebtMaturitiesWithinOneYearFairValue"],
    "应收账款": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "存货": ["InventoryNet"],
    "其他流动资产": ["OtherAssetsCurrent"],
    "流动资产合计": ["AssetsCurrent"],
    # GOOGL 自 FY2025 起把 PP&E 换成含融资租赁使用权的标签：
    # PropertyPlantAndEquipmentNet 停在 2024-12-31（171,036），
    # 之后是 ...AndFinanceLeaseRightOfUseAssetAfter... （246,597）。
    # 少这一条，2,466 亿美元会被「其他非流动资产」倒挤吸收，而平衡校验照样是 0。
    "固定资产净额": ["PropertyPlantAndEquipmentNet",
                     "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
                     "PropertyPlantAndEquipmentNetExcludingFinanceLeaseRightOfUseAsset"],
    "经营租赁使用权资产": ["OperatingLeaseRightOfUseAsset"],
    "长期投资": ["LongTermInvestments", "MarketableSecuritiesNoncurrent",
                 "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
                 "AvailableForSaleSecuritiesNoncurrent", "OtherLongTermInvestments"],
    # 非上市股权投资：GOOGL $641 亿、NVDA $223 亿，是权益桥里的真实非经营资产，
    # 不单列就会被倒挤进「其他非流动资产」，从而在估值里凭空消失。
    "非上市股权投资": ["EquitySecuritiesWithoutReadilyDeterminableFairValueAmount",
                       "EquitySecuritiesFVNINoncurrent"],
    "权益法投资": ["EquityMethodInvestments"],
    "商誉": ["Goodwill"],
    "无形资产": ["FiniteLivedIntangibleAssetsNet", "IntangibleAssetsNetExcludingGoodwill"],
    "其他非流动资产": ["OtherAssetsNoncurrent"],
    "资产总计": ["Assets"],
    "应付账款": ["AccountsPayableCurrent", "AccountsPayableAndAccruedLiabilitiesCurrent"],
    "其他流动负债": ["OtherLiabilitiesCurrent", "AccruedLiabilitiesCurrent"],
    "一年内到期债务": ["LongTermDebtCurrent", "DebtCurrent"],
    "短期借款": ["ShortTermBorrowings", "CommercialPaper", "OtherShortTermBorrowings"],
    "流动负债合计": ["LiabilitiesCurrent"],
    "长期债务": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "经营租赁负债_非流动": ["OperatingLeaseLiabilityNoncurrent"],
    "其他非流动负债": ["OtherLiabilitiesNoncurrent"],
    "负债合计": ["Liabilities"],
    "留存收益": ["RetainedEarningsAccumulatedDeficit"],
    "归母权益": ["StockholdersEquity"],
    "少数股东权益": ["MinorityInterest"],
    # 夹层权益：可赎回少数股东权益列在负债与权益【之间】，不计入就会让恒等式差一截
    "夹层权益": ["TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests",
                 "RedeemableNoncontrollingInterestEquityCarryingAmount",
                 "TemporaryEquityCarryingAmountAttributableToParent"],
    "权益合计": ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
                 "StockholdersEquity"],
}
TAGS_CF = {
    "经营活动现金流": ["NetCashProvidedByUsedInOperatingActivities",
                       "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    # D&A 必须拆开。MSFT / GOOGL 根本没有 DepreciationDepletionAndAmortization 这个标签，
    # 只标 Depreciation（纯固定资产折旧）+ 另行单标无形摊销与融资租赁摊销。
    # 若把 Depreciation 当作合计用，MSFT FY2025 会少算 122 亿美元（−35.6%，交叉校验当场抓出）。
    "折旧摊销合计": ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
                     "DepreciationAndAmortization"],
    "折旧": ["Depreciation"],
    "无形摊销": ["AmortizationOfIntangibleAssets"],
    "融资租赁摊销": ["FinanceLeaseRightOfUseAssetAmortization"],
    "股份支付": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "资本开支": ["PaymentsToAcquirePropertyPlantAndEquipment",
                 "PaymentsToAcquireProductiveAssets",
                 "PaymentsToAcquirePropertyPlantAndEquipmentAndIntangibleAssets",
                 "PaymentsForCapitalImprovements"],
    "回购": ["PaymentsForRepurchaseOfCommonStock"],
    "分红": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
}
TAGS_SHARES = {
    "摊薄股数": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "基本股数": ["WeightedAverageNumberOfSharesOutstandingBasic",
                 "WeightedAverageNumberOfSharesOutstanding"],
}


def _derive_bs(bal: pd.DataFrame, diag: dict) -> None:
    """把四个「其他」科目改成【倒挤项】，让资产负债表按构造恒等。

    直接用 OtherAssetsCurrent / OtherLiabilitiesCurrent 这类标签会漏科目：
    AAPL 的流动资产里还有「非贸易应收」等未被任何常用标签覆盖的部分，
    实测缺口 241 亿美元 —— 三表会以一个【恒定偏移】的形式不平，且不报错。

    倒挤之后，恒等式由报表自己的小计与合计保证，缺哪个明细都不影响平衡。
    代价是「其他」项里混进了一些本可单列的科目 —— 这是刻意的取舍：
    宁可科目粒度粗一点，也不要一张不平的表。"""
    # 小计推导必须在早退检查【之前】：AMZN 等公司根本不申报 Liabilities，
    # 若先做 need 检查就会直接 return，推导代码永远执行不到。
    if "负债合计" not in bal.columns or bal["负债合计"].isna().any():
        if {"资产总计", "权益合计"} <= set(bal.columns):
            mez = bal["夹层权益"].fillna(0.0) if "夹层权益" in bal.columns else 0.0
            derived = bal["资产总计"] - bal["权益合计"].fillna(0.0) - mez
            bal["负债合计"] = (bal["负债合计"].fillna(derived)
                               if "负债合计" in bal.columns else derived)
            diag.setdefault("推导", []).append("负债合计 = 资产总计 − 权益合计（AMZN 等不申报 Liabilities）")
    # AVGO 的 StockholdersEquity 停在 2019，之后只申报「含少数股东权益」的口径。
    if "归母权益" not in bal.columns or bal["归母权益"].isna().any():
        if "权益合计" in bal.columns:
            mi = bal["少数股东权益"].fillna(0.0) if "少数股东权益" in bal.columns else 0.0
            derived = bal["权益合计"] - mi
            bal["归母权益"] = (bal["归母权益"].fillna(derived)
                               if "归母权益" in bal.columns else derived)
            diag.setdefault("推导", []).append("归母权益 = 权益合计 − 少数股东权益（AVGO 等只申报含少数股东口径）")
    need = ["流动资产合计", "资产总计", "流动负债合计", "负债合计"]
    if not all(c in bal.columns for c in need):
        diag.setdefault("警告", []).append("缺少报表小计，无法倒挤「其他」科目，平衡校验可能不为 0")
        return
    z = lambda c: bal[c].fillna(0.0) if c in bal.columns else 0.0
    reported = {k: (bal[k].copy() if k in bal.columns else None)
                for k in ("其他流动资产", "其他非流动资产", "其他流动负债", "其他非流动负债")}
    bal["其他流动资产"] = bal["流动资产合计"] - (z("现金及等价物") + z("短期投资")
                                                + z("应收账款") + z("存货"))
    bal["其他非流动资产"] = (bal["资产总计"] - bal["流动资产合计"]
                             - (z("固定资产净额") + z("长期投资") + z("商誉") + z("无形资产")))
    bal["其他流动负债"] = bal["流动负债合计"] - (z("应付账款") + z("一年内到期债务") + z("短期借款"))
    bal["其他非流动负债"] = bal["负债合计"] - bal["流动负债合计"] - z("长期债务")
    diag.setdefault("推导", []).append("其他流动/非流动 资产与负债：由报表小计倒挤，保证恒等")
    _guard_plug(bal, reported, diag)


def _guard_plug(bal: pd.DataFrame, reported: dict, diag: dict) -> None:
    """倒挤项的护栏 —— 数据层最重要的检错点。

    为什么不能靠「★平衡校验 = 0」：倒挤让恒等式【按构造】成立。实测 GOOGL FY2025
    的 2,466 亿美元 PP&E 整个丢失（该年起改用含融资租赁的标签），平衡校验依然是 0.0。

    也不能简单地拿倒挤值和申报的「其他」标签比大小 —— 倒挤项【本来就】会包含
    非贸易应收（AAPL 约 328 亿）、递延收入（MSFT 约 730 亿）、非上市股权（GOOGL 约 641 亿）
    这些未单列的科目，它们是真实的经营性余额，不是错误。

    真正的失败模式是【跨年换标签】：某个大科目在某一年之后突然进不了标签链，
    于是倒挤项出现一次结构性跳变。所以检的是【比重的年度突变】，不是绝对水平。"""
    JUMP_PP, JUMP_ABS, LEVEL = 0.08, 10_000.0, 0.60      # 8pp / 百万美元 / 占比上限
    base = {"其他流动资产": "资产总计", "其他非流动资产": "资产总计",
            "其他流动负债": "负债合计", "其他非流动负债": "负债合计"}
    bad = []
    for k, denom in base.items():
        if k not in bal.columns or denom not in bal.columns:
            continue
        share = (bal[k] / bal[denom]).astype(float)
        for a, b in zip(bal.index[:-1], bal.index[1:]):
            if pd.isna(share.get(a)) or pd.isna(share.get(b)):
                continue
            d = share[b] - share[a]
            if d > JUMP_PP and (bal[k][b] - bal[k][a]) > JUMP_ABS:
                bad.append(f"{k} 占{denom}比重 {a} {share[a]:.1%} → {b} {share[b]:.1%}"
                           f"（跳变 {d:+.1%}，金额 {bal[k][b] - bal[k][a]:+,.0f}）")
        for e in bal.index:
            if pd.notna(share.get(e)) and share[e] > LEVEL:
                bad.append(f"{e} {k} 占{denom} {share[e]:.1%}，超过 {LEVEL:.0%} 上限")
    if bad:
        # 只警告不阻断：跳变既可能是丢标签，也可能是真实的资产负债表扩张
        # （NVDA FY2026 营收 130→216B，流动资产随之暴涨，属后者）。
        # 硬失败的职责在 assert_complete —— 关键科目缺期才是可确证的错误。
        diag.setdefault("倒挤项异动（请人工确认不是丢标签）", []).extend(bad[:8])


def _derive_da(cfs: pd.DataFrame, diag: dict, fmp_da: dict | None = None) -> None:
    """D&A 的两个用途需要两个不同口径，不能混为一谈：

      · 【折旧】用于校准「折旧率 = 折旧 ÷ 期初固定资产净额」→ 必须是纯固定资产折旧，
        对应 XBRL 的 Depreciation（PP&E 附注口径）。
      · 【折旧摊销合计】是 FCFF 的非现金加回项 → 必须是现金流量表那一行。

    MSFT / GOOGL 压根不标 DepreciationDepletionAndAmortization，只标 Depreciation；
    而它们现金流量表的那一行叫「Depreciation, amortization, and other」，
    比 Depreciation 大得多（MSFT FY2025：22,000 vs 34,153）。两者是不同概念，不是谁错。
    所以合计口径的取数优先级是：聚合标签 → FMP 的现金流量表行 → 分项相加（并标记为估算）。"""
    for c in ("折旧摊销合计", "折旧", "无形摊销", "融资租赁摊销"):
        if c not in cfs.columns:
            cfs[c] = np.nan
    if fmp_da:
        fill = cfs["折旧摊销合计"].isna() & pd.Series({e: e in fmp_da for e in cfs.index})
        if fill.any():
            cfs.loc[fill, "折旧摊销合计"] = [fmp_da[e] for e in cfs.index[fill]]
            diag.setdefault("推导", []).append("折旧摊销合计（EDGAR 无聚合标签，取自 FMP 现金流量表行）")
    part = cfs[["折旧", "无形摊销"]].fillna(0.0).sum(axis=1)
    has_part = cfs[["折旧", "无形摊销"]].notna().any(axis=1)
    need = cfs["折旧摊销合计"].isna() & has_part
    if need.any():
        cfs.loc[need, "折旧摊销合计"] = part[need]
        diag.setdefault("推导", []).append("折旧摊销合计（估算：折旧+无形摊销，可能少计现金流量表的「及其他」）")
    back = cfs["折旧"].isna() & cfs["折旧摊销合计"].notna()
    if back.any():
        cfs.loc[back, "折旧"] = (cfs.loc[back, "折旧摊销合计"]
                                 - cfs.loc[back, ["无形摊销", "融资租赁摊销"]].fillna(0.0).sum(axis=1))
        diag.setdefault("推导", []).append("折旧（合计扣摊销分项）")
    cfs["摊销"] = cfs["折旧摊销合计"] - cfs["折旧"]


# 关键科目：缺任何一个，估值链路都会静默走偏 —— 宁可不出模型，也不出错的模型。
CRITICAL = ["营业收入", "营业成本", "经营利润", "净利润",
            "现金及等价物", "固定资产净额", "资产总计", "负债合计", "归母权益"]


def assert_complete(inc, bal, cfs, ends, diag) -> None:
    """硬失败门槛。用户明确选了「宁可不出，不出错的」。"""
    frames = {"income": inc, "balance": bal, "cashflow": cfs}
    bad = []
    for name in CRITICAL:
        col = next((f[name] for f in frames.values() if name in f.columns), None)
        if col is None:
            bad.append(f"{name}：一期都取不到")
            continue
        miss = [e for e in ends if pd.isna(col.get(e))]
        if miss:
            bad.append(f"{name}：{len(miss)}/{len(ends)} 期缺失（{'、'.join(miss)}）")
    if bad:
        raise DataError(
            "关键科目不完整，拒绝出模型：\n  · " + "\n  · ".join(bad) +
            "\n\n多半是该公司用了本模板标签链未覆盖的 us-gaap 标签。"
            "\n排查：python3 -c \"import us_data as D;g=D.facts(D.resolve('TICKER')['cik']);"
            "print([t for t in g if '关键词' in t])\" 找到实际标签后补进 us_data.py 的 TAGS_* 链。")


def statements(g: dict, ticker: str, nyears: int = 5, splits: list | None = None) -> dict:
    """归一化三表。返回 DataFrame（行=期末日，列=科目，单位百万美元）+ 抽取诊断。
    splits 给定时不再自行取拆股记录（上游由 Tiingo 日线的 splitFactor 提取）。"""
    sp = splits if splits is not None else split_history(ticker)
    rev = series(g, TAGS_IS["营业收入"])
    if not rev:
        raise DataError("取不到任何年度营业收入 —— 该公司可能不按 us-gaap 申报，或刚上市。")
    ends = sorted(rev)[-nyears:]
    if len(ends) < 3:
        raise DataError(f"仅有 {len(ends)} 个完整财年（需要至少 3 年）。上市时间可能不足。")

    diag = {"缺失": [], "拆股": sp}

    def build(tagmap, duration, scale=MN, splits=None):
        cols = {}
        for name, tags in tagmap.items():
            col = pick_series(g, tags, ends, duration, scale, splits, diag, name)
            if col is not None:
                cols[name] = col
        return pd.DataFrame(cols, index=ends)

    inc = build(TAGS_IS, True)
    bal = build(TAGS_BS, False)
    cfs = build(TAGS_CF, True)
    _derive_da(cfs, diag, fmp_cashflow_da(ticker, ends))
    _fill_cogs_from_fmp(inc, ticker, ends, diag)
    _derive_bs(bal, diag)                          # 内含小计推导 + 倒挤 + 异动警告
    assert_complete(inc, bal, cfs, ends, diag)     # 关键科目缺期 → 硬失败
    shr = build(TAGS_SHARES, True, scale=MN, splits=sp)   # 股数按拆股还原，单位百万股
    return dict(income=inc, balance=bal, cashflow=cfs, shares=shr, ends=ends, diag=diag)


# ============================================================
# 行情 / 市场参数
# ============================================================
def quote(ticker: str) -> dict:
    """现价 + 在外股数。yfinance 为主 —— Stooq 现在对无 JS 的客户端返回验证页，
    而那个页面恰好是 796 字节的正常 200 响应：只看状态码或字节数的连通性测试会误判为可用。
    （我第一版的连通性测试就是这么错的，这里保留教训。）"""
    errs = []
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        px = fi.get("lastPrice")
        if px:
            return dict(source="yfinance", price=float(px),
                        shares=float(fi["shares"]) / MN if fi.get("shares") else np.nan,
                        mktcap=float(fi["marketCap"]) / MN if fi.get("marketCap") else np.nan)
        errs.append("yfinance: 无 lastPrice")
    except Exception as e:
        errs.append(f"yfinance: {type(e).__name__}")
    try:
        px = _stooq_csv(f"https://stooq.com/q/l/?s={ticker.lower()}.us&f=sd2t2ohlcv&h&e=csv")
        if px is not None and "Close" in px.columns:
            return dict(source="Stooq", price=float(px["Close"].iloc[-1]), shares=np.nan, mktcap=np.nan)
        errs.append("Stooq: 返回非 CSV（多半是 JS 验证页）")
    except Exception as e:
        errs.append(f"Stooq: {type(e).__name__}")
    raise DataError("行情通道全部失败：" + "；".join(errs))


def _stooq_csv(url: str):
    """Stooq 返回值必须验载荷，不能只看状态码/字节数。"""
    import io
    t = _get(url, {"User-Agent": "Mozilla/5.0"}).decode("utf-8", "replace")
    if not t.lstrip().lower().startswith(("date,", "symbol,")):
        return None
    return pd.read_csv(io.StringIO(t))


def shares_outstanding(cik: str) -> float:
    """在外普通股股数（百万股），取 dei 的最新申报值。比行情源更贴近财报口径。"""
    d = _cached(f"cf_{cik}.json", lambda: _json(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"))
    dei = d.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
    if not dei:
        return np.nan
    arr = [x for u in dei.get("units", {}).values() for x in u if x.get("val")]
    if not arr:
        return np.nan
    best = max(arr, key=lambda x: (x.get("end", ""), x.get("filed", "")))
    return float(best["val"]) / MN


def prices(ticker: str, years: int = 6) -> pd.DataFrame:
    """日线收盘（已复权）。"""
    import yfinance as yf
    h = yf.Ticker(ticker).history(period=f"{years}y", interval="1d", auto_adjust=True)
    if h is None or h.empty:
        d = _stooq_csv(f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d")
        if d is None:
            raise DataError(f"取不到 {ticker} 的日线（yfinance 空，Stooq 返回验证页）。")
        d["Date"] = pd.to_datetime(d["Date"])
        return d.set_index("Date")[["Close"]].sort_index()
    h.index = pd.to_datetime(h.index).tz_localize(None)
    return h[["Close"]]


def index_prices(symbol: str = "^GSPC", years: int = 6) -> pd.DataFrame:
    """标普 500 日线，用作 beta 回归的市场基准。"""
    import yfinance as yf
    h = yf.Ticker(symbol).history(period=f"{years}y", interval="1d", auto_adjust=True)
    if h is None or h.empty:
        raise DataError(f"取不到指数 {symbol} 的日线。")
    h.index = pd.to_datetime(h.index).tz_localize(None)
    return h[["Close"]]


def riskfree() -> tuple[float, str]:
    """10 年期美国国债收益率（FRED DGS10，无需 key）。"""
    try:
        t = _get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
                 {"User-Agent": "Mozilla/5.0"}).decode()
        rows = [r.split(",") for r in t.strip().split("\n")[1:]]
        rows = [r for r in rows if len(r) == 2 and r[1] not in (".", "")]
        d, v = rows[-1]
        return float(v) / 100, d
    except Exception:
        return 0.0425, "取数失败，使用常量 4.25%（请自行核对）"


def _fill_cogs_from_fmp(inc: pd.DataFrame, ticker: str, ends: list[str], diag: dict) -> None:
    """ORCL 等公司近年不在 XBRL 单列营业成本（只有 CostsAndExpenses 合计），
    但模板以毛利率驱动，没有它就建不了模。此时从 FMP 补位，并显式记进诊断 ——
    关键科目换源必须让使用者看得见，不能悄悄发生。"""
    if "营业成本" in inc.columns and not inc["营业成本"].isna().any():
        return
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return
    try:
        arr = _json(f"https://financialmodelingprep.com/stable/income-statement"
                    f"?symbol={ticker}&period=annual&limit=5&apikey={key}", {"User-Agent": "Mozilla/5.0"})
    except Exception:
        return
    got = {}
    for e in ends:
        hit = next((x for x in arr if abs((pd.Timestamp(x["date"]) - pd.Timestamp(e)).days) <= 3), None)
        if hit and hit.get("costOfRevenue"):
            got[e] = float(hit["costOfRevenue"]) / MN
    if not got:
        return
    cur = inc["营业成本"] if "营业成本" in inc.columns else pd.Series(np.nan, index=inc.index)
    inc["营业成本"] = [cur.get(e) if pd.notna(cur.get(e)) else got.get(e) for e in inc.index]
    diag.setdefault("换源", []).append(
        f"营业成本 {len(got)}/{len(ends)} 期取自 FMP（XBRL 未单列，只有 CostsAndExpenses 合计）")


def fmp_cashflow_da(ticker: str, ends: list[str]) -> dict | None:
    """FMP 的现金流量表 D&A 行，仅在 EDGAR 缺聚合标签时用作补位。"""
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return None
    try:
        arr = _json(f"https://financialmodelingprep.com/stable/cash-flow-statement"
                    f"?symbol={ticker}&period=annual&limit=5&apikey={key}", {"User-Agent": "Mozilla/5.0"})
    except Exception:
        return None
    out = {}
    for e in ends:
        hit = next((x for x in arr if abs((pd.Timestamp(x["date"]) - pd.Timestamp(e)).days) <= 3), None)
        if hit and hit.get("depreciationAndAmortization"):
            out[e] = float(hit["depreciationAndAmortization"]) / MN
    return out or None


# ============================================================
# Damodaran 隐含股权风险溢价
# ============================================================
# 他每月初重算并发布。口径不止一个，默认取 ERP (T12m) —— 即他每月对外引用的那个。
ERP_VARIANTS = {
    "T12m": "ERP (T12m)",
    "T12m_payout": "ERP (T12 m with sustainable payout)",
    "T12m_adj_rf": "ERP (T12m) with adj riskfree rate",
    "normalized": "ERP (Normalized)",
    "smoothed": "ERP (Smoothed)",
    "net_cash_yield": "ERP (Net Cash Yield)",
}
ERP_URL = "https://pages.stern.nyu.edu/~adamodar/pc/implprem/ERPbymonth.xlsx"
ERP_FALLBACK = 0.0409      # 2026-09-01 的 ERP (T12m)，取数失败时的兜底


def damodaran_erp(variant: str = "T12m") -> dict:
    """抓取 Damodaran 的月度隐含股权风险溢价。

    为什么用隐含 ERP 而不是 5% 的实务惯例值：隐含 ERP 是【由市场当前价格反解出来】的
    折现率溢价。用高于它的 ERP 去给个股估值，等于在结构上保证「看什么都贵」——
    实测把 ERP 从 5.0% 降到市场隐含值，四只大盘科技股的低估幅度平均收窄约 10pp。
    5% 仍作为可选项保留（见假设页注释）。

    载荷必须验内容不验字节数：这个 URL 若被改成 404 页或验证页，
    仍会返回 200 + 可观的字节数（Stooq 的教训）。"""
    import io
    try:
        raw = _cached_bytes("damodaran_erp.xlsx", lambda: _get(ERP_URL, {"User-Agent": "Mozilla/5.0"}),
                            ttl_hours=24 * 7)
        if raw[:2] != b"PK":                      # XLSX 必须是 zip
            raise ValueError("返回的不是 XLSX（多半是错误页）")
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(raw), data_only=True)["Historical ERP"]
        hdr = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
        col = hdr.get(ERP_VARIANTS.get(variant, ERP_VARIANTS["T12m"]))
        tb = hdr.get("T.Bond Rate")
        if not col:
            raise ValueError(f"找不到列「{ERP_VARIANTS.get(variant)}」")
        for r in range(ws.max_row, 1, -1):        # 从最后一行往回找第一个有值的月份
            v, d = ws.cell(r, col).value, ws.cell(r, 1).value
            if isinstance(v, (int, float)) and 0.0 < v < 0.20 and d is not None:
                alts = {k: ws.cell(r, hdr[lab]).value for k, lab in ERP_VARIANTS.items()
                        if lab in hdr and isinstance(ws.cell(r, hdr[lab]).value, (int, float))}
                return dict(erp=round(float(v), 4), date=str(d)[:10], variant=variant,
                            tbond=ws.cell(r, tb).value if tb else None,
                            alts=alts, source="Damodaran ERPbymonth.xlsx")
        raise ValueError("表内没有可用的 ERP 数值")
    except Exception as e:
        return dict(erp=ERP_FALLBACK, date="取数失败", variant=variant, tbond=None, alts={},
                    source=f"⚠ 抓取失败（{type(e).__name__}），使用兜底常量 {ERP_FALLBACK:.2%}，请自行核对")


def _cached_bytes(name: str, fn, ttl_hours: int = 24):
    os.makedirs(_CACHE, exist_ok=True)
    p = os.path.join(_CACHE, name)
    if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < ttl_hours * 3600:
        with open(p, "rb") as f:
            return f.read()
    b = fn()
    with open(p, "wb") as f:
        f.write(b)
    return b


# ============================================================
# 交叉校验：EDGAR vs FMP
# ============================================================
def cross_check(ticker: str, st: dict, tol: float = 0.005) -> dict:
    """双源比对。这不是锦上添花 —— 实测中它能立刻抓出「标签选错→整列为 0」这类静默错误。"""
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return dict(ok=None, note="未设置 FMP_API_KEY，跳过交叉校验（强烈建议配置）", rows=[])
    base = "https://financialmodelingprep.com/stable"
    try:
        inc = _json(f"{base}/income-statement?symbol={ticker}&period=annual&limit=5&apikey={key}",
                    {"User-Agent": "Mozilla/5.0"})
        cfs = _json(f"{base}/cash-flow-statement?symbol={ticker}&period=annual&limit=5&apikey={key}",
                    {"User-Agent": "Mozilla/5.0"})
    except Exception as e:
        msg = str(e)
        if "402" in msg or "not available under your current subscription" in msg:
            note = ("FMP 免费档不覆盖该标的（HTTP 402），交叉校验【不可用】—— "
                    "注意这不等于「一致」，本次财报数据仅有 EDGAR 单一来源")
        else:
            note = f"FMP 调用失败：{type(e).__name__}（交叉校验不可用，非「一致」）"
        return dict(ok=None, note=note, rows=[])
    fi = {x["date"]: x for x in inc}
    fc = {x["date"]: x for x in cfs}
    PAIRS = [("营业收入", "income", "revenue", fi), ("经营利润", "income", "operatingIncome", fi),
             ("净利润", "income", "netIncome", fi), ("研发费用", "income", "researchAndDevelopmentExpenses", fi),
             ("资本开支", "cashflow", "capitalExpenditure", fc),
             ("折旧摊销合计", "cashflow", "depreciationAndAmortization", fc),
             ("股份支付", "cashflow", "stockBasedCompensation", fc),
             ("经营活动现金流", "cashflow", "netCashProvidedByOperatingActivities", fc)]
    rows = []
    for end in st["ends"]:
        src = fi.get(end)
        if src is None:      # FMP 的 date 偶尔差一天，容忍 ±3 天
            cand = [d for d in fi if abs((pd.Timestamp(d) - pd.Timestamp(end)).days) <= 3]
            if not cand:
                rows.append(dict(期末=end, 科目="—", 状态="FMP 无此期"))
                continue
            end_f = cand[0]
        else:
            end_f = end
        for name, sheet, fkey, book in PAIRS:
            if name not in st[sheet].columns:
                continue
            a = st[sheet].loc[end, name]
            b = book.get(end_f, {}).get(fkey)
            if b is None or a is None or (isinstance(a, float) and np.isnan(a)):
                continue
            b = abs(float(b)) / MN if name in ("资本开支", "回购", "分红") else float(b) / MN
            a = abs(a) if name in ("资本开支", "回购", "分红") else a
            if abs(b) < 1e-9 and abs(a) < 1e-9:
                continue
            den = max(abs(a), abs(b), 1.0)
            diff = abs(a - b) / den
            if diff > tol:
                rows.append(dict(期末=end, 科目=name, EDGAR=round(a, 1), FMP=round(b, 1),
                                 偏差=f"{diff:.1%}"))
    return dict(ok=not rows, note="EDGAR 与 FMP 全部一致" if not rows else f"{len(rows)} 处不一致",
                rows=rows)


# ============================================================
# 中期锚定：年报之后已披露的季度
# ============================================================
# 为什么必须有：AAPL 的年报期末距今 349 天，其后已披露 3 个季度、
# 合计营收 = 基数年的 88%，而模型把那一年整个当成【纯预测】——
# 在已知事实面前继续猜。实测把这 3 个季度用上，每股由 $124.91 → $136.15（+9.0%）。
#
# 与 A 股的差别：A 股中期报告是【累计】数，美股 10-Q 给的是【单季】数，
# 所以这里按单季求和，而不是直接取累计值。
def _quarter_facts(g: dict, tags: list[str]) -> dict:
    """所有【单季】事实 {(start, end): 值}，跨标签合并、同期取申报日最新。"""
    out = {}
    for t in tags:
        for x in g.get(t, {}).get("units", {}).get("USD", []):
            if not x.get("start") or not x.get("form", "").startswith(("10-Q", "10-K")):
                continue
            if not (2 <= _months(x["start"], x["end"]) <= 4):
                continue
            k = (x["start"][:10], x["end"][:10])
            if k not in out or x.get("filed", "") > out[k][1]:
                out[k] = (x["val"] / MN, x.get("filed", ""))
    return {k: v[0] for k, v in out.items()}


def single_quarters_for(q_prev: list, fy_prev, q_cur: list) -> tuple[list, list]:
    """首年锚定用的单季序列：去年四季（Q4 = 全年 − 前三季 —— 公司不为第四季度单报 10-Q）、
    本年已公布的 1–3 季。本年已满四季说明新年报应已发布，不再用季报反推全年。"""
    if len(q_cur) >= 4:
        raise ValueError("本年已披露满四个季度 —— 新年报应已发布，不用季报反推全年")
    q4 = (fy_prev - sum(q_prev)) if fy_prev is not None and len(q_prev) == 3 and None not in q_prev else None
    return list(q_prev) + [q4], list(q_cur)


QUARTER_DAYS = 365.25 / 4
_QTOL = 20                 # 季度起止日的容差（天），覆盖 52/53 周财年


def _by_position(qf: dict, fy_start: dt.date, nmax: int = 4) -> list:
    """单季事实按**期间**对位到财年第 1..nmax 季：第 k 季的起点应在财年起点 + (k−1) 季附近。
    对不上的位置为 None。不按排序位置认 —— 去年缺第二季时，第三季会被当成第二季（审查发现）。"""
    out = [None] * nmax
    for (s, e), v in qf.items():
        ds = (dt.date.fromisoformat(s) - fy_start).days
        span = (dt.date.fromisoformat(e) - dt.date.fromisoformat(s)).days
        k = round(ds / QUARTER_DAYS) + 1
        if 1 <= k <= nmax and abs(ds - (k - 1) * QUARTER_DAYS) <= _QTOL and 70 <= span <= 110:
            out[k - 1] = v
    return out


def interim_block(g: dict, ends: list[str]) -> dict | None:
    """年报之后的已披露季度（按期间对位）+ 去年四季，供首年锚定；历史同期占比只作参考。
    年报之后一个季度都没有返回 None；本年已满四季时返回带 skip 原因的块（不截断照常锚定）。"""
    last = ends[-1]
    qrev = _quarter_facts(g, TAGS_IS["营业收入"])
    qcog = _quarter_facts(g, TAGS_IS["营业成本"])
    arev = series(g, TAGS_IS["营业收入"])
    acog = series(g, TAGS_IS["营业成本"])
    day = dt.timedelta(days=1)
    cur_start = dt.date.fromisoformat(last) + day
    cur_r = _by_position({k: v for k, v in qrev.items() if k[1] > last}, cur_start)
    cur_c = _by_position({k: v for k, v in qcog.items() if k[1] > last}, cur_start)
    seen = [i for i, v in enumerate(cur_r) if v is not None]
    if not seen:
        return None
    nq = seen[-1] + 1
    after = sorted([k for k in qrev if k[1] > last], key=lambda k: k[1])
    if nq >= 4:
        return dict(n_q=4, skip="本年已披露满四个季度 —— 新年报应已发布，不用季报反推全年",
                    first=after[0][0], last=after[-1][1], label="前 4 个季度")

    prev_lo, prev_hi = (ends[-2], ends[-1]) if len(ends) >= 2 else (None, None)
    q_prev_rev = q_prev_cogs = [None] * 4
    if prev_lo:
        p_start = dt.date.fromisoformat(prev_lo) + day
        inside = lambda qf: {k: v for k, v in qf.items() if prev_lo < k[1] <= prev_hi}
        q_prev_rev, q_prev_cogs = _by_position(inside(qrev), p_start), _by_position(inside(qcog), p_start)
        for qs, ann in ((q_prev_rev, arev.get(prev_hi)), (q_prev_cogs, acog.get(prev_hi))):
            if qs[3] is None and ann is not None and None not in qs[:3]:
                qs[3] = ann - sum(qs[:3])          # 公司不为第四季度单报 10-Q：全年 − 前三季
    q_cur_rev, q_cur_cogs = cur_r[:nq], cur_c[:nq]

    rev_i = sum(v for v in q_cur_rev if v is not None)
    have = None not in q_cur_rev and None not in q_cur_cogs
    cog_i = sum(q_cur_cogs) if have else None
    # 〔参考〕历史同期占比：每个财年前 nq 季 ÷ 全年（不进生效值；算不出时不影响锚定）
    sh_hist = []
    for i in range(1, len(ends)):
        lo, hi = ends[i - 1], ends[i]
        qs = _by_position({k: v for k, v in qrev.items() if lo < k[1] <= hi},
                          dt.date.fromisoformat(lo) + day)[:nq]
        if None not in qs and arev.get(hi):
            sh_hist.append(sum(qs) / arev[hi])
    share = float(np.mean(sh_hist)) if sh_hist else None
    std = float(np.std(sh_hist)) if sh_hist else None
    yoy_fy = None
    if prev_lo and None not in q_prev_rev[:nq] and have and sum(q_prev_rev[:nq]) > 0 and arev.get(prev_hi):
        yoy_fy = arev[prev_hi] * (rev_i / sum(q_prev_rev[:nq]))
    return dict(n_q=nq, first=after[0][0], last=after[-1][1], stable=bool(std is not None and std <= 0.03),
                q_prev_rev=q_prev_rev, q_prev_cogs=q_prev_cogs,
                q_cur_rev=q_cur_rev, q_cur_cogs=q_cur_cogs,
                implied_fy_yoy=round(yoy_fy, 2) if yoy_fy else None,
                rev=round(rev_i, 2), cogs=round(cog_i, 2) if have else None,
                gm=round(1 - cog_i / rev_i, 5) if have and rev_i else None,
                share=round(share, 4) if share else None,
                share_hist=[round(x, 4) for x in sh_hist],
                share_std=round(std, 4) if std is not None else None,
                label=f"前 {nq} 个季度",
                implied_fy=round(rev_i / share, 2) if share else None)


# ============================================================
# 国别风险溢价 + 收入地域加权 ERP
# ============================================================
# 为什么要做：AAPL 国际收入约 57%、GOOGL 约 52%，全部按【美国】ERP 折现，
# 等于假设在中国、欧洲赚的钱和在美国赚的钱风险一样。
#
# 口径（Damodaran 2025 年起的新惯例）：他已不再把美国当作成熟市场基准
# （美国不再是 Aaa，自身带 0.23% 的国别风险溢价）。所以：
#     成熟市场基准 = 标普 500 隐含 ERP − 美国 CRP
#     地域加权 ERP = 成熟市场基准 + Σ(收入权重 × 该国/区域 CRP)
# 纯美国收入时自动还原为标普隐含 ERP —— 与原行为无缝衔接。
CTRYPREM_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/ctryprem.xlsx"


def country_risk_premiums() -> dict:
    """Damodaran 国别风险溢价。区域值按 GDP 加权（简单平均会被小国拉偏）。"""
    import io, collections
    try:
        raw = _cached_bytes("ctryprem.xlsx", lambda: _get(CTRYPREM_URL, {"User-Agent": "Mozilla/5.0"}),
                            ttl_hours=24 * 30)
        if raw[:2] != b"PK":
            raise ValueError("返回的不是 XLSX")
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), data_only=True)
        ws = wb["ERPs by country"]
        by_c, reg_of = {}, {}
        for r in range(8, ws.max_row + 1):
            c, reg, crp = ws.cell(r, 1).value, ws.cell(r, 2).value, ws.cell(r, 6).value
            if isinstance(c, str) and isinstance(crp, (int, float)):
                by_c[c.strip().upper()] = float(crp)
                reg_of[c.strip().upper()] = (reg or "").strip()
        # 区域 CRP：用 GDP 加权
        gdp = {}
        if "Regional Weighted Averages" in wb.sheetnames:
            w2 = wb["Regional Weighted Averages"]
            for r in range(2, w2.max_row + 1):
                c, g_ = w2.cell(r, 1).value, w2.cell(r, 2).value
                if isinstance(c, str):
                    try:
                        gdp[c.strip().upper()] = float(g_)
                    except (TypeError, ValueError):
                        pass
        agg = collections.defaultdict(lambda: [0.0, 0.0])
        for c, crp in by_c.items():
            reg = reg_of.get(c) or "?"
            w_ = gdp.get(c, 0.0)
            agg[reg][0] += crp * w_
            agg[reg][1] += w_
        by_r = {reg: (num / den if den > 0 else None) for reg, (num, den) in agg.items()}
        # GDP 权重缺失的区域退回中位数
        import numpy as _np
        for reg in list(by_r):
            if by_r[reg] is None:
                xs = [v for c, v in by_c.items() if reg_of.get(c) == reg]
                by_r[reg] = float(_np.median(xs)) if xs else 0.0
        # 「非美国」桶：全球 GDP 加权（剔除美国）。
        # 不能用各区域 CRP 的简单平均 —— 那会把非洲和西欧等权重，
        # 实测让 MSFT 的 ERP 虚高 1.6pp（非美收入其实绝大部分在西欧与发达亚洲）。
        num = den = 0.0
        for c, crp in by_c.items():
            if c == "UNITED STATES":
                continue
            w_ = gdp.get(c, 0.0)
            num += crp * w_; den += w_
        exus = (num / den) if den > 0 else float(_np.median(list(by_c.values())))
        return dict(by_country=by_c, by_region=by_r, region_of=reg_of, exus_crp=exus,
                    us_crp=by_c.get("UNITED STATES", 0.0), source="Damodaran ctryprem.xlsx")
    except Exception as e:
        return dict(by_country={}, by_region={}, region_of={}, us_crp=0.0, exus_crp=0.0,
                    source=f"⚠ 抓取失败（{type(e).__name__}），退回纯美国 ERP")


# FMP 的分部名是公司自拟的，必须归一化。映射不确定时【标记为未映射】并交给使用者，
# 绝不静默套一个数字上去。
_GEO_COUNTRY = {
    "UNITED STATES": "UNITED STATES", "US": "UNITED STATES", "U.S.": "UNITED STATES",
    "USA": "UNITED STATES", "CHINA": "CHINA", "GREATER CHINA": "CHINA",
    "TAIWAN, PROVINCE OF CHINA": "TAIWAN", "TAIWAN": "TAIWAN", "JAPAN": "JAPAN",
    "SINGAPORE": "SINGAPORE", "GERMANY": "GERMANY", "IRELAND": "IRELAND",
    "UNITED KINGDOM": "UNITED KINGDOM", "INDIA": "INDIA", "KOREA, REPUBLIC OF": "KOREA",
    "CANADA": "CANADA", "MEXICO": "MEXICO", "BRAZIL": "BRAZIL",
}
_GEO_REGION = {
    "EUROPE": "Western Europe", "EMEA": "Western Europe", "WESTERN EUROPE": "Western Europe",
    "ASIA PACIFIC": "Asia", "REST OF ASIA PACIFIC": "Asia", "ASIA": "Asia",
    "AMERICAS": "North America", "OTHER AMERICAS": "Central and South America",
    "AMERICAS EXCLUDING UNITED STATES": "Central and South America",
    "LATIN AMERICA": "Central and South America", "MIDDLE EAST": "Middle East",
    "AFRICA": "Africa",
}


def _norm_segment(name: str) -> str:
    s = str(name).upper().replace("SEGMENT", "").replace("&", "AND")
    return " ".join(s.replace(",", " , ").split()).replace(" , ", ", ").strip(" ,")


def map_segment(name: str) -> tuple[str, str | None]:
    """分部名 → ('country'|'region'|'exus'|'unmapped', 键)。"""
    s = _norm_segment(name)
    if s in _GEO_COUNTRY:
        return "country", _GEO_COUNTRY[s]
    if s in _GEO_REGION:
        return "region", _GEO_REGION[s]
    if s.replace(" ", "") in ("NONUS", "NONUS.", "INTERNATIONAL", "RESTOFWORLD", "OTHERCOUNTRIES"):
        return "exus", None
    for k, v in _GEO_COUNTRY.items():
        if k in s:
            return "country", v
    for k, v in _GEO_REGION.items():
        if k in s:
            return "region", v
    return "unmapped", None


def revenue_geography(ticker: str) -> dict | None:
    """FMP 的分地域收入。免费档不覆盖该标的时返回 None（降级为纯美国）。"""
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return None
    try:
        arr = _json(f"https://financialmodelingprep.com/stable/revenue-geographic-segmentation"
                    f"?symbol={ticker}&apikey={key}", {"User-Agent": "Mozilla/5.0"})
    except Exception:
        return None
    if not arr or not isinstance(arr, list):
        return None
    row = arr[0]
    data = {k: float(v) for k, v in (row.get("data") or {}).items()
            if isinstance(v, (int, float)) and v > 0}
    tot = sum(data.values())
    if tot <= 0:
        return None
    return dict(fy=row.get("fiscalYear"), date=row.get("date"),
                weights={k: v / tot for k, v in data.items()})


def geo_weighted_erp(ticker: str, implied_erp: float) -> dict:
    """收入地域加权 ERP。任何一步取不到就退回纯美国 ERP，并显式说明原因。"""
    crp = country_risk_premiums()
    geo = revenue_geography(ticker)
    base = implied_erp - crp["us_crp"]           # 成熟市场基准
    if not geo or not crp["by_country"]:
        return dict(erp=implied_erp, base=base, rows=[], ok=False,
                    note=("分地域收入取不到（FMP 免费档不覆盖该标的或未配置 key）"
                          if not geo else f"国别溢价表不可用：{crp['source']}") +
                         " —— 退回纯美国 ERP，这不等于该公司没有国别风险")
    # 「非美国」桶用全球 GDP 加权（除美国外）的 CRP 近似
    exus = crp.get("exus_crp", 0.0)
    rows, wsum, unmapped = [], 0.0, 0.0
    for name, w in sorted(geo["weights"].items(), key=lambda kv: -kv[1]):
        kind, key = map_segment(name)
        if kind == "country":
            c = crp["by_country"].get(key.upper())
        elif kind == "region":
            c = crp["by_region"].get(key)
        elif kind == "exus":
            c = exus
        else:
            c = None
        if c is None:
            unmapped += w
            rows.append(dict(segment=name, weight=w, mapped="⚠ 未映射", crp=None))
        else:
            wsum += w * c
            rows.append(dict(segment=name, weight=w, mapped=key or ("非美国加权" if kind == "exus" else "?"),
                             crp=round(c, 5)))
    if unmapped > 0:            # 未映射部分按已映射部分的加权 CRP 补，并显式提示
        wsum += unmapped * (wsum / max(1e-9, 1 - unmapped))
    return dict(erp=round(base + wsum, 5), base=round(base, 5), rows=rows, ok=True,
                unmapped=round(unmapped, 4), weighted_crp=round(wsum, 5),
                fy=geo.get("fy"), note=crp["source"])
