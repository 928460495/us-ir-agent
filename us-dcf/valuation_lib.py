# -*- coding: utf-8 -*-
"""估值方法库 —— 美股 DCF 模板专用。

方法来源：FINC6021 Corporate Valuation（University of Sydney, S2 2025，Dr DuckKi Cho），
教材 Titman & Martin, *Valuation: The Art and Science of Corporate Investment Decisions*, 3rd ed.

本模块只放【与会计准则无关】的估值方法。信用利差用的是 ICE BofA 美元利差，
只适用于美元计价的模型 —— 课程公式 kd = 无风险利率 + 国家违约利差 + 公司违约利差
要求利差与无风险利率同币种，套到人民币模型上是错的。

本包与 ashare-dcf 完全独立，不 import 也不修改 A 股版的任何文件。

  1. Altman Z-score → 合成信用评级 → 实时信用利差 → 债务成本 kd（Ch.4 讲义 p.13–17）
     ——替代原来硬编码的 kd = rf + 120bp。
  2. 盈亏平衡敏感性（Ch.3 讲义 p.22–25）：求令估值等于现价的临界值与偏离百分比。
     ——与龙卷风图回答的是不同问题，见下方 breakeven_table 的注释。
  3. 去杠杆 / 再杠杆 β（Ch.4）：可比公司法，也是「目标资本结构」口径的基础。
  4. 可持续增长率 g = (1 − 派息率) × ROE（公式表）：终值 g 的独立交叉校验。
  5. 经营杠杆度 DOL（Ch.8 公式表）。
  6. 资本结构漂移检测：WACC 假定资本结构恒定（Ch.4 三条估计原则之三），
     但以现金为 plug 的三表模型会让 D/(D+E) 逐年漂移 —— 这是模型内部的一致性缺口。
"""
from __future__ import annotations
import urllib.request
import numpy as np

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
_UA = {"User-Agent": "Mozilla/5.0"}

# ============================================================
# 1. Altman Z-score → 合成评级 → 信用利差
# ============================================================
# 讲义 p.15「Average Z-Score by S&P Bond Rating, S&P 500: 1996–2001」。
# 讲义只给各评级的【均值】，未给分档边界；这里取相邻评级均值的中点作为阈值，
# 是对该表最直接的还原，没有额外自由度。
Z_BY_RATING = [("AAA", 6.20), ("AA", 4.73), ("A", 3.74), ("BBB", 2.81),
               ("BB", 2.38), ("B", 1.80), ("CCC及以下", 0.33)]

# ICE BofA 期权调整利差（OAS），FRED 实时、免费、无需 key。
# 讲义用的是 2017 年的静态曲线图；换成实时序列是同一方法的更好实现。
SPREAD_SERIES = {"AAA": "BAMLC0A1CAAA", "AA": "BAMLC0A2CAA", "A": "BAMLC0A3CA",
                 "BBB": "BAMLC0A4CBBB", "BB": "BAMLH0A1HYBB", "B": "BAMLH0A2HYB",
                 "CCC及以下": "BAMLH0A3HYC"}


def altman_z(working_capital: float, retained_earnings: float, ebit: float,
             mkt_equity: float, total_liabilities: float, sales: float,
             total_assets: float) -> float:
    """Z = 1.2A + 1.4B + 3.3C + 0.6D + 1.0E（讲义 p.14，变量定义完整的标准版）。

    讲义另提到中国版 Z（Hu, Shi, Wang & Yu, 2020, JBF），但只给了系数
    0.517 − 0.460·X1 + 9.320·X2 + 0.388·X3 + 1.158·X4 而【没有定义 X1–X4】，
    故此处一律用标准 Altman。"""
    if not total_assets or not total_liabilities:
        return float("nan")
    A = working_capital / total_assets
    B = retained_earnings / total_assets
    C = ebit / total_assets
    D = mkt_equity / total_liabilities
    E = sales / total_assets
    return 1.2 * A + 1.4 * B + 3.3 * C + 0.6 * D + 1.0 * E


def z_decomposition(working_capital, retained_earnings, ebit, mkt_equity,
                    total_liabilities, sales, total_assets) -> dict:
    """把 Z 拆成五项，看清是谁在驱动。

    对大市值公司，D 项（市值/总负债 × 0.6）会压倒性主导 —— 实测 AAPL 81%、
    MSFT 80%、GOOGL 85%、NVDA 93%。Altman 1968 年是在制造业样本上标定的，
    那时市值与负债是同一量级；NVDA 的市值是总负债的 168 倍，Z=70 并不表示
    「比 AAA 安全 13 倍」，只表示这个比值很大。
    所以 Z 高不等于评级准 —— 合成评级在投资级顶端几乎没有分辨力。"""
    if not total_assets or not total_liabilities:
        return {}
    terms = {"A 营运资本/总资产": 1.2 * working_capital / total_assets,
             "B 留存收益/总资产": 1.4 * retained_earnings / total_assets,
             "C EBIT/总资产": 3.3 * ebit / total_assets,
             "D 股权市值/总负债": 0.6 * mkt_equity / total_liabilities,
             "E 销售/总资产": 1.0 * sales / total_assets}
    z = sum(terms.values())
    return dict(terms={k: round(v, 3) for k, v in terms.items()}, z=round(z, 3),
                d_share=round(terms["D 股权市值/总负债"] / z, 4) if z else None)


def wacc_sensitivity_to_kd(wd: float, tax: float, dkd: float = 0.0077) -> float:
    """kd 变动 dkd 时 WACC 的变动。杠杆极低时这个数会小到无关紧要 ——
    实测 AAPL（Wd 2.10%）仅 1.28bp、NVDA（Wd 0.16%）仅 0.10bp。
    换句话说：合成评级这套方法值不值得较真，取决于 Wd，而不取决于 Z 算得多精。"""
    return dkd * wd * (1 - tax)


def z_zone(z: float) -> str:
    """Altman 原始的三档困境判别（与评级映射是两回事，各有各的用途）。"""
    if not np.isfinite(z):
        return "无法计算"
    if z > 2.99:
        return "安全区"
    if z >= 1.81:
        return "灰色区"
    return "困境区"


def synthetic_rating(z: float) -> str:
    """Z → S&P 合成评级。阈值 = 讲义表中相邻评级均值的中点。"""
    if not np.isfinite(z):
        return "BBB"          # 取不到时给投资级中枢，且调用方会标记为「默认值」
    for i, (r, mu) in enumerate(Z_BY_RATING):
        if i + 1 == len(Z_BY_RATING):
            return r
        cut = (mu + Z_BY_RATING[i + 1][1]) / 2
        if z >= cut:
            return r
    return Z_BY_RATING[-1][0]


def _fred_latest(sid: str) -> tuple[float, str] | None:
    try:
        t = urllib.request.urlopen(
            urllib.request.Request(FRED.format(sid), headers=_UA), timeout=20).read().decode()
        rows = [r.split(",") for r in t.strip().split("\n")[1:]]
        rows = [r for r in rows if len(r) == 2 and r[1] not in (".", "")]
        return float(rows[-1][1]) / 100, rows[-1][0]
    except Exception:
        return None


def credit_spread(rating: str) -> tuple[float, str]:
    """该评级的实时信用利差。取不到则退回一张静态兜底表，并在说明里讲清楚。"""
    sid = SPREAD_SERIES.get(rating)
    if sid:
        hit = _fred_latest(sid)
        if hit:
            return hit[0], f"ICE BofA {rating} OAS（FRED {sid}，{hit[1]}）"
    fallback = {"AAA": .0045, "AA": .0060, "A": .0070, "BBB": .0100,
                "BB": .0155, "B": .0275, "CCC及以下": .1050}
    return fallback.get(rating, .0100), f"{rating} 静态兜底利差（FRED 取数失败，请自行核对）"


def cost_of_debt(rf: float, z: float, country_spread: float = 0.0) -> dict:
    """kd = 无风险利率 + 国家违约利差 + 公司违约利差（讲义 p.13 的公式）。"""
    rating = synthetic_rating(z)
    sp, src = credit_spread(rating)
    return dict(kd=round(rf + country_spread + sp, 6), rating=rating, spread=sp,
                zone=z_zone(z), z=round(float(z), 3) if np.isfinite(z) else None, source=src)


def implied_cost_of_debt(interest_expense: float, st_debt: float, lt_debt: float) -> float:
    """讲义的「第二种方法」：利息费用 ÷（短期有息负债 + 长期负债）。
    完全可从公开报表算出，适合私营企业；这里用作合成评级法的交叉校验。"""
    d = (st_debt or 0) + (lt_debt or 0)
    return float(interest_expense) / d if d else float("nan")


# ============================================================
# 2. 去杠杆 / 再杠杆 β
# ============================================================
def unlever_beta(beta_equity: float, debt: float, equity: float,
                 beta_debt: float = 0.0) -> float:
    """β_u = [β_E + β_D·(D/E)] / [1 + (D/E)]（公式表）。等价于市值加权。"""
    if equity <= 0:
        return float("nan")
    de = debt / equity
    return (beta_equity + beta_debt * de) / (1 + de)


def relever_beta(beta_unlevered: float, debt: float, equity: float,
                 beta_debt: float = 0.0) -> float:
    """β_E = β_u + (D/E)(β_u − β_D)（讲义 Ch.4）。"""
    if equity <= 0:
        return float("nan")
    return beta_unlevered + (debt / equity) * (beta_unlevered - beta_debt)


# ============================================================
# 3. 增长与杠杆的交叉校验
# ============================================================
def sustainable_growth(payout_ratio: float, roe: float) -> float:
    """g = (1 − 派息率) × ROE（公式表）。

    这是【股权侧】的增长锚；模板终值用的是 Damodaran 的【企业侧】g = 再投资率 × ROIC。
    两者口径不同，但若差距很大，说明终值的增长、再投资与回报三者不自洽。"""
    return (1 - payout_ratio) * roe


def sustainable_growth_is_meaningful(roe: float, equity: float, assets: float) -> tuple[bool, str]:
    """这条公式对【长期大额回购】的公司会失效，必须先判定再用。

    公式的逻辑是「留存的利润按 ROE 再投资，因而带来增长」。但库存股是按【市价】
    冲减权益的，一家长期高价回购的公司，账面权益会被冲到极小甚至为负 ——
    此时 ROE 高得离谱，却不代表任何可再投资的资本回报。

    实测：AAPL FY2025 归母权益仅 737 亿美元，ROE 171%，派息率 15.2%
    → g_sustainable = 145%。这个数字没有任何经济含义。
    这类公司应以【投入资本回报率 ROIC】为锚（即模板终值用的 g = 再投资率 × ROIC）。"""
    if not np.isfinite(roe):
        return False, "ROE 无法计算"
    if roe > 0.50:
        return False, f"ROE {roe:.0%} > 50%，多半是长期回购把账面权益冲小所致，公式失效"
    if assets and equity / assets < 0.15:
        return False, f"权益/总资产 {equity/assets:.1%} < 15%，账面权益已不足以代表可再投资资本"
    if roe < 0:
        return False, "ROE 为负"
    return True, ""


def dol(fixed_costs: float, ocf: float, tax: float | None = None,
        depreciation: float | None = None) -> float:
    """经营杠杆度。无税版 DOL = 1 + FC/OCF；含税折旧版 = 1 + [FC(1−t) − Dep·t]/OCF。

    讲义的两栋写字楼案例：固定成本占比更高 → 营业利润对收入更敏感 → 风险更高
    → 理应用更低的倍数。naive 套倍数会高估 7%。"""
    if not ocf:
        return float("nan")
    if tax is None or depreciation is None:
        return 1 + fixed_costs / ocf
    return 1 + (fixed_costs * (1 - tax) - depreciation * tax) / ocf


def capital_structure_drift(debt_path: list[float], equity_mktval_path: list[float],
                            wacc_weight: float) -> dict:
    """WACC 的三条估计原则之三是「用前瞻性的权重与成本，WACC 假定资本结构恒定」。
    但以现金为 plug 的三表模型会让 D/(D+E) 逐年漂移。这里把漂移量算出来，
    让使用者看见「模型内含的资本结构」和「WACC 里假设的那个」差多少。"""
    w = [d / (d + e) if (d + e) else float("nan") for d, e in zip(debt_path, equity_mktval_path)]
    fin = [x for x in w if np.isfinite(x)]
    return dict(path=[round(x, 4) for x in w],
                assumed=round(wacc_weight, 4),
                max_gap=round(max(abs(x - wacc_weight) for x in fin), 4) if fin else float("nan"))


# ============================================================
# 4. 盈亏平衡敏感性
# ============================================================
def breakeven(f, lo: float, hi: float, target: float = 0.0, tol: float = 1e-7,
              max_iter: int = 200) -> float:
    """二分求 f(x) = target 的解。f 必须在 [lo, hi] 上单调且变号。

    讲义推荐用 Excel 的 Goal Seek；在 Python 侧用二分是同一件事，
    好处是能一次跑完所有驱动因子，并且结果可复现。"""
    a, b = lo, hi
    fa, fb = f(a) - target, f(b) - target
    if not (np.isfinite(fa) and np.isfinite(fb)) or fa * fb > 0:
        return float("nan")
    for _ in range(max_iter):
        m = (a + b) / 2
        fm = f(m) - target
        if not np.isfinite(fm):
            return float("nan")
        if abs(fm) < tol or (b - a) < tol:
            return m
        if fa * fm < 0:
            b, fb = m, fm
        else:
            a, fa = m, fm
    return (a + b) / 2


def breakeven_table(drivers: dict, evaluate, target: float) -> list[dict]:
    """对每个驱动因子求临界值与偏离百分比，按「越小越关键」排序。

    drivers: {名称: (期望值, 下界, 上界, 设置函数)}，设置函数接受一个值、返回估值结果。
    target : 令模型结论翻转的阈值（股票估值里通常取【现价】，对应讲义的 NPV=0）。

    ⚠ 这与龙卷风图回答的不是同一个问题：
        龙卷风  = 在我假设的不确定性范围内，这个变量能让价值波动多少（离散度）
        盈亏平衡 = 我要错到什么程度，结论才会翻转（脆弱度）
    一个变量可以龙卷风条很长但临界值很远（结论稳健），也可以条很短但临界值近在咫尺
    （结论脆弱）。两个都要看。"""
    rows = []
    for name, (exp, lo, hi, setter) in drivers.items():
        crit = breakeven(lambda x: evaluate(setter, x), lo, hi, target)
        if not np.isfinite(crit):
            rows.append(dict(变量=name, 期望值=exp, 临界值=None, 偏离=None,
                             说明="在给定区间内不变号（该变量单独无法把估值推到现价）"))
            continue
        pct = (crit - exp) / abs(exp) if exp else float("nan")
        rows.append(dict(变量=name, 期望值=round(exp, 6), 临界值=round(crit, 6),
                         偏离=round(pct, 6) if np.isfinite(pct) else None, 说明=""))
    rows.sort(key=lambda r: abs(r["偏离"]) if r["偏离"] is not None else 9e9)
    return rows


# ============================================================
# 研发资本化（Damodaran）
# ============================================================
# 摊销年限按行业。Damodaran 的经验表：软件/互联网 3 年，半导体与计算机硬件 5 年，
# 制药/生物 10 年，通信设备 10 年。用 SIC 前两/四位映射。
RD_LIFE_BY_SIC = {
    "7372": 3, "7370": 3, "7371": 3, "7373": 3, "7374": 3, "7379": 3,   # 软件与计算机服务
    "3674": 5, "3559": 5, "3827": 5,                                     # 半导体及设备
    "3571": 5, "3572": 5, "3576": 5, "3577": 5,                          # 计算机硬件
    "3661": 10, "3663": 10, "3669": 10,                                  # 通信设备
    "2834": 10, "2836": 10, "8731": 10, "2833": 10,                      # 制药 / 生物 / 研究
    "3711": 10, "3714": 10,                                              # 汽车及零部件
}


def rd_life(sic: str, default: int = 5) -> int:
    """研发资本的摊销年限。"""
    sic = str(sic or "").strip()
    if sic in RD_LIFE_BY_SIC:
        return RD_LIFE_BY_SIC[sic]
    if sic[:2] == "73":
        return 3
    if sic[:2] == "28" or sic[:2] == "87":
        return 10
    return default


def capitalize_rd(rd_hist: list[float], life: int) -> dict:
    """把历史研发费用还原成一项资产。

    rd_hist 按时间升序，最后一个是当期。
    · 研发资本 = 各年未摊销部分之和：t-i 年的支出还剩 (life-i)/life
    · 当期摊销 = 过去 life 年各摊 1/life（当期支出当年不摊）
    · 调整后经营利润 = 报告经营利润 + 当期研发费用 − 当期摊销

    返回值里的 covered 说明历史年数够不够：不足 life 年时研发资本会【系统性低估】，
    因为更早的支出取不到。5 年财报 + 10 年摊销期（制药）就属于这种情况，必须提示。"""
    rd = [float(x) for x in rd_hist if x is not None and x == x]
    if not rd:
        return dict(asset=0.0, amort=0.0, life=life, covered=0, complete=False)
    asset = sum(rd[-1 - i] * (life - i) / life for i in range(min(life, len(rd))))
    amort = sum(rd[-1 - i] / life for i in range(1, min(life + 1, len(rd))))
    return dict(asset=round(asset, 2), amort=round(amort, 2), life=life,
                covered=len(rd), complete=len(rd) >= life + 1)


def invested_capital(o: dict) -> dict:
    """投入资本的两种口径。

    经营端 = 营运资本 + 固定资产 + 商誉无形 + 其他非流动资产 − 其他非流动负债
    融资端 = 有息债务 + 全部权益 + 夹层权益 − 现金 − 短期投资 − 长期投资

    【重要澄清】这两侧【恒等】，因为它们是同一张资产负债表恒等式的两种重排 ——
    所以对账为 0 并不构成独立的检错能力，只是口径定义的自洽性展示。
    真正的检错在数据层（关键科目缺期硬失败）。

    原口径 IC = 营运资本 + 固定资产 对美股大盘会崩：AAPL 营运资本 −520 亿、
    商誉无形与其他非流动资产未计入 → IC = −22 亿 → ROIC −4760%，终值被迫降级。"""
    op = (o["nwc"] + o["ppe"] + o.get("gwi", 0.0)
          + o.get("onca", 0.0) - o.get("oncl", 0.0))
    fin = (o["debt"] + o["equity"] + o.get("mezz", 0.0)
           - o["cash"] - o.get("sti", 0.0) - o.get("lti", 0.0))
    return dict(operating=round(op, 2), financing=round(fin, 2),
                gap=round(op - fin, 4))
