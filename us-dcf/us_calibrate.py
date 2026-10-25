# -*- coding: utf-8 -*-
"""美股 DCF 模板 — 假设校准层。从历史财报推导可编辑的假设，单位百万美元。

与 A 股版的分工一致：本层只做「从历史推导数字」，不写 Excel、不做估值判断。

FINC6021 方法在本层的落点：
  · 债务成本 kd 不再拍脑袋 —— Altman Z-score → 合成评级 → ICE BofA 实时利差（Ch.4）
  · β 同时给 5 年与 2 年两个窗口 —— 课程明确把窗口长度列为判断项（Ch.4）
  · 可持续增长率 g = (1−派息率)×ROE 作为终值 g 的独立交叉校验（公式表）
  · 经营杠杆度 DOL —— 解释为什么表面相似的公司理应有不同倍数（Ch.8）
"""
from __future__ import annotations
import datetime as dt
import numpy as np, pandas as pd
import us_data as D
import valuation_lib as VL


def _mean(xs, default=0.0, n=3):
    xs = [float(x) for x in xs if x is not None and np.isfinite(x)]
    return float(np.mean(xs[-n:])) if xs else default


def _safe(df, col, idx, default=0.0):
    if col not in df.columns:
        return default
    v = df.loc[idx, col]
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return default if not np.isfinite(v) else v


def _beta(px: pd.DataFrame, mkt: pd.DataFrame, years: int) -> tuple[float, float, int]:
    a = px["Close"].pct_change().dropna()
    b = mkt["Close"].pct_change().dropna()
    j = pd.concat([a, b], axis=1, join="inner").tail(252 * years)
    j.columns = ["s", "m"]
    if len(j) < 60 or j["m"].var() == 0:
        return 1.0, 1.0, len(j)
    raw = float(np.cov(j["s"], j["m"])[0, 1] / np.var(j["m"]))
    # Blume 调整：β 长期向 1 均值回归
    return round(0.67 * raw + 0.33, 4), round(raw, 4), len(j)


def _fade(g0: float, g_term: float, n: int, fade_n: int | None = None) -> list[float]:
    """默认增速路径：从最近一年实际增速【线性衰减】至永续增速。

    ⚠ fade_n 与 n 必须解耦。曾经 fade_n 就等于 n（明确期年数），
    于是 `--years 5 → 10` 不只是多建模 5 年，而是把衰减期整整拉长一倍 ——
    实测 NVDA 因此从 −39% 翻到 +121%，一个开关让结论完全反转且无任何提示。
    衰减期是「竞争优势能维持多久」的判断（核心假设）；明确期只是建模粒度（技术选择）。

    A 股版用「历史均值 × 全期不变」，对中国工业股够用；套到美股高成长股上会失控 ——
    实测 NVDA 近 4 年增速均值约 102%，平推 5 年后营收会到 7.2 万亿美元，
    这个数字不是保守也不是激进，只是没有意义。

    线性衰减是【起点】不是预测：它仍然是黄底判断项，三档同值，请自行拉开。
    课程 Ch.6 的立场是收入预测要自上而下（市场规模×份额）或自下而上（产品线加总），
    任何机械外推都只是占位符。"""
    fade_n = n if fade_n is None else max(1, min(int(fade_n), n))
    if n <= 1:
        return [round(g_term, 4)]
    if fade_n <= 1:
        return [round(g_term, 4)] * n
    head = [round(g0 + (g_term - g0) * i / (fade_n - 1), 4) for i in range(fade_n)]
    return head + [round(g_term, 4)] * (n - fade_n)


# 首年按季度锚定的规则 —— 自 ashare_calibrate 原样移植（只换科目名与金额单位），
# 两个引擎同一条规则。
G_RANGE = (-0.75, 2.0)   # 年初至今同比的合理范围（用户决定，2026-10-19）


def quarterly_estimate(prev_rev, prev_cogs, cur_rev, cur_cogs) -> tuple[dict | None, str]:
    """首个预测年按季度估算。返回 (估算, 不锚定的原因)；能估算时原因为空串。

    **规则只在这里定义一份**：工作簿第三小节用活公式实现同一规则，us_verify
    独立重算，ir-agent 只读本函数写进中期块的结果。

    - 已公布的季度用实际；
    - 未公布的季度 = 去年同季 × (1 + 本年已公布季度的年初至今同比)，营收与营业
      成本**各用各的同比**（保留各自的季节性）；
    - 首年三档相同：它是实际 + 机械外推，不是判断，情景分化从第 2 年开始。

    取代此前的「已披露 ÷ 近几年同期占比（最小/均值/最大）」三档锚定（2026-10-18，
    用户决定）。旧规则对周期股失灵：牧原股份近 4 年上半年占比 0.355～0.530，用
    均值 0.44 反推全年营收约 1346 百万美元，按三季度销售简报推算只有约 1185 百万美元，
    高估 13.6%，且首年是后续年份复合的基数。

    **不锚定**（首年回到增速判断项）的情形，原因如实写出：
    - 本年没有已公布季度；去年任一季度、本年任一已公布季度缺数据；
    - 参与计算的任一季度不为正（终审 2026-10-19：报表追溯重述后「全年 − 前三季」
      可能为负，外推出负的季度营收）；
    - 营收或营业成本的年初至今同比超出 `G_RANGE`（去年同季基数极小时同比可达
      +9900%，全年营收凭空膨胀几十倍）。

    `prev_*`：去年四个单季；`cur_*`：本年已公布的 k 个单季（1 ≤ k ≤ 3）。
    """
    k = len(cur_rev)
    if not 1 <= k <= 3 or len(cur_cogs) != k:
        return None, "本年没有已公布的季度"
    if len(prev_rev) != 4 or len(prev_cogs) != 4:
        return None, "去年季度数据不全"
    for who, rows in (("去年", (("营业收入", prev_rev), ("营业成本", prev_cogs))),
                      ("本年", (("营业收入", cur_rev), ("营业成本", cur_cogs)))):
        for item, seq in rows:
            for i, x in enumerate(seq):
                if x is None:
                    return None, f"{who} Q{i + 1} {item}缺失"
                if x <= 0:
                    return None, f"{who} Q{i + 1} {item}不为正（{x:.2f} 百万美元）"
    g_rev = sum(cur_rev) / sum(prev_rev[:k]) - 1
    g_cogs = sum(cur_cogs) / sum(prev_cogs[:k]) - 1
    lo, hi = G_RANGE
    for item, g in (("营收", g_rev), ("营业成本", g_cogs)):
        if not lo <= g <= hi:
            return None, (f"{item}年初至今同比 {g:+.1%} 超出 {lo:.0%}～+{hi:.0%} 的合理范围"
                          "（去年同季基数过小或数据异常）")
    est_rev = list(cur_rev) + [x * (1 + g_rev) for x in prev_rev[k:]]
    est_cogs = list(cur_cogs) + [x * (1 + g_cogs) for x in prev_cogs[k:]]
    return dict(k=k, g_rev=g_rev, g_cogs=g_cogs, est_rev=est_rev, est_cogs=est_cogs,
                fy_rev=sum(est_rev), fy_cogs=sum(est_cogs)), ""



def calibrate(symbol: str, nyears: int = 5, fade_years: int | None = None, *,
              stmts: dict | None = None, quote: dict | None = None,
              px: pd.DataFrame | None = None, mkt: pd.DataFrame | None = None) -> dict:
    """校准。上游（ir-agent）可注入数据，注入的部分不再自行取数：

    - stmts：与 `D.statements` 同形状的报表（事实账本供数，分析层与估值层同源）；
    - quote：`dict(source, price)`（账本里的现价，FMP → Tiingo）；
    - px / mkt：个股与标普 500 的日线收盘（列名 Close，索引为日期），β 回归用。

    独立使用时仍走 yfinance → Stooq（Stooq 现为 JS 验证页，yfinance 需另装）。
    """
    meta = D.resolve(symbol)
    D.assert_non_financial(meta)
    g = D.facts(meta["cik"])
    st = stmts if stmts is not None else D.statements(g, meta["ticker"], nyears)
    inc, bal, cfs, shr = st["income"], st["balance"], st["cashflow"], st["shares"]
    ends = st["ends"]
    last = ends[-1]

    q = quote if quote is not None else D.quote(meta["ticker"])
    sh_dei = D.shares_outstanding(meta["cik"])
    shares = sh_dei if np.isfinite(sh_dei) else q.get("shares", np.nan)
    if not np.isfinite(shares):
        raise D.DataError("取不到在外股数（dei 与行情源均无），无法折算每股价值。")
    mktcap = q["price"] * shares

    rf, rfd = D.riskfree()
    erp_i = D.damodaran_erp()          # 市场隐含 ERP，每月更新
    itm = D.interim_block(g, ends)     # 年报之后已披露的季度（中期锚定用）
    itm_skip = None
    if itm:
        qe, itm_skip = quarterly_estimate(itm["q_prev_rev"], itm["q_prev_cogs"],
                                          itm["q_cur_rev"], itm["q_cur_cogs"])
        if qe:          # 不能叫 q —— q 是上面的行情
            itm.update(k=qe["k"], g_rev=qe["g_rev"], g_cogs=qe["g_cogs"], est_rev=qe["est_rev"],
                       est_cogs=qe["est_cogs"], fy_rev=qe["fy_rev"], fy_cogs=qe["fy_cogs"])
            itm_skip = None
        else:
            itm["fy_rev"] = itm["fy_cogs"] = None
    geo = D.geo_weighted_erp(symbol, erp_i["erp"])   # 收入地域加权 ERP
    px = px if px is not None else D.prices(meta["ticker"])
    mkt = mkt if mkt is not None else D.index_prices()
    beta5, raw5, n5 = _beta(px, mkt, 5)
    beta2, raw2, n2 = _beta(px, mkt, 2)

    rev = inc["营业收入"]
    R = lambda col, sheet=inc: [
        (_safe(sheet, col, e) / rev[e]) if rev[e] else np.nan for e in ends]

    # ---------- 利润表结构 ----------
    gm_hist = [1 - _safe(inc, "营业成本", e) / rev[e] for e in ends]
    rd = _mean(R("研发费用"))
    # SG&A 的标签在各家之间不统一：合并标签优先，缺失时用「销售+管理」两项相加
    sga_hist = []
    for e in ends:
        v = _safe(inc, "销售及管理费用", e, np.nan)
        if not np.isfinite(v) or v == 0:
            v = _safe(inc, "销售费用", e) + _safe(inc, "管理费用", e)
        sga_hist.append(v / rev[e] if rev[e] else np.nan)
    sga = _mean(sga_hist)
    # 其余经营性损益全部收进残差项，保证历史经营利润【精确】对上报表。
    # 这样做的好处是：任何没被上面几项捕捉到的科目（重组、减值、诉讼和解……）
    # 都不会凭空消失，也不会被误当成利润。
    resid_hist = []
    for e, gmv, sg in zip(ends, gm_hist, sga_hist):
        gp = rev[e] * gmv
        rdv = _safe(inc, "研发费用", e)
        sgv = (sg if np.isfinite(sg) else 0) * rev[e]
        resid_hist.append((_safe(inc, "经营利润", e) - (gp - rdv - sgv)) / rev[e] if rev[e] else np.nan)
    resid = _mean(resid_hist)

    tax_hist = []
    for e in ends:
        pt = _safe(inc, "税前利润", e, np.nan)
        if not np.isfinite(pt) or pt == 0:
            pt = _safe(inc, "经营利润", e) - _safe(inc, "利息支出", e)
        tx = _safe(inc, "所得税", e, np.nan)
        if np.isfinite(pt) and pt > 0 and np.isfinite(tx):
            tax_hist.append(tx / pt)
    taxr = float(np.clip(_mean(tax_hist, .21), .05, .35))

    ni_all = [_safe(inc, "净利润", e) for e in ends]
    nci = [_safe(inc, "少数股东损益", e) for e in ends]
    mi_share = (nci[-1] / ni_all[-1]) if ni_all[-1] else 0.0

    # ---------- 营运资本（天数口径）----------
    def days(col, base_col, sheet=bal, mult=365):
        out = []
        for e in ends:
            b = _safe(inc, base_col, e)
            v = _safe(sheet, col, e, np.nan)
            if b and np.isfinite(v):
                out.append(v / b * mult)
        return out
    dso = _mean(days("应收账款", "营业收入"), 45)
    dio = _mean(days("存货", "营业成本"), 40)
    dpo = _mean(days("应付账款", "营业成本"), 60)
    ocap = _mean(R("其他流动资产", bal), .03)
    oclp = _mean(R("其他流动负债", bal), .10)

    # ---------- 折旧 / 摊销 / 资本开支 / SBC ----------
    # 折旧率的分母用【期初】固定资产净额；分子用纯 PP&E 折旧（us_data 已把
    # 折旧与无形摊销拆开）。A 股版分子含无形摊销而分母只有固定资产，口径不严；此处不重蹈。
    dep_rate = []
    for i in range(1, len(ends)):
        p0 = _safe(bal, "固定资产净额", ends[i - 1])
        dp = _safe(cfs, "折旧", ends[i], np.nan)
        if p0 and np.isfinite(dp):
            dep_rate.append(dp / p0)
    deprate = _mean(dep_rate, .15)
    amort = _mean(R("摊销", cfs), .01)
    capex = _mean(R("资本开支", cfs), .05)
    sbc = _mean(R("股份支付", cfs), .02)
    buyback = _mean(R("回购", cfs), .0)
    div_paid = _mean([(_safe(cfs, "分红", e) / ni_all[i]) if ni_all[i] else np.nan
                      for i, e in enumerate(ends)], .20)
    payout = float(np.clip(div_paid, 0, 1))

    # ---------- 增长与毛利的历史统计 ----------
    g_hist = [float(rev[ends[i]] / rev[ends[i - 1]] - 1) for i in range(1, len(ends))]
    gm_mean = float(np.mean(gm_hist[-3:]))
    _rho = 0.0
    if len(g_hist) >= 3:
        gm_al = gm_hist[1:]
        if np.std(g_hist) > 0 and np.std(gm_al) > 0:
            _rho = float(np.corrcoef(g_hist, gm_al)[0, 1])

    # ---------- 期初余额 ----------
    o = {}
    for k, col in [("cash", "现金及等价物"), ("sti", "短期投资"), ("ar", "应收账款"),
                   ("inv", "存货"), ("oca", "其他流动资产"), ("ppe", "固定资产净额"),
                   ("lti", "长期投资"), ("gw", "商誉"), ("intan", "无形资产"),
                   ("onca", "其他非流动资产"), ("ap", "应付账款"), ("ocl", "其他流动负债"),
                   ("cur", "一年内到期债务"), ("std", "短期借款"), ("ltd", "长期债务"),
                   ("oncl", "其他非流动负债"), ("mi", "少数股东权益"), ("eq", "归母权益"),
                   ("re", "留存收益"), ("ta", "资产总计"), ("tl", "负债合计"),
                   ("ca", "流动资产合计"), ("cl", "流动负债合计"),
                   ("rou", "经营租赁使用权资产"), ("lease_nc", "经营租赁负债_非流动")]:
        o[k] = _safe(bal, col, last)
    o["mezz"] = _safe(bal, "夹层权益", last)
    o["_check"] = o["ta"] - o["tl"] - o["eq"] - o["mi"] - o["mezz"]

    # ---------- 研发资本化（Damodaran）----------
    # 取【全部可得年度】而非只取建模用的 5 年：摊销期 5 年需要 6 期支出，
    # 10 年（制药）需要 11 期。年数不足会系统性低估研发资本，complete 标志会提示。
    _rd_all = D.series(g, D.TAGS_IS["研发费用"])
    _life = VL.rd_life(meta["sic"])
    rdcap = VL.capitalize_rd([_rd_all[k] for k in sorted(_rd_all) if k <= last], _life)
    o["rd_asset"] = rdcap["asset"]

    # ---------- 投入资本：两种口径 ----------
    o["nwc"] = o["ar"] + o["inv"] + o["oca"] - o["ap"] - o["ocl"]
    o["gwi"] = o["gw"] + o["intan"]
    ic = VL.invested_capital(dict(nwc=o["nwc"], ppe=o["ppe"], gwi=o["gwi"],
                                  onca=o["onca"], oncl=o["oncl"],
                                  debt=o["std"] + o["cur"] + o["ltd"],
                                  equity=o["eq"] + o["mi"], mezz=o["mezz"],
                                  cash=o["cash"], sti=o["sti"], lti=o["lti"]))
    o["ic_reported"] = ic["operating"]
    o["ic_rdcap"] = ic["operating"] + rdcap["asset"]

    debt = o["std"] + o["cur"] + o["ltd"]
    wd = float(min(max(debt / (debt + mktcap), 0.0), 0.60)) if (debt + mktcap) > 0 else 0.0

    # ---------- FINC6021：Z-score → 合成评级 → 实时利差 → kd ----------
    z = VL.altman_z(working_capital=o["ca"] - o["cl"], retained_earnings=o["re"],
                    ebit=_safe(inc, "经营利润", last), mkt_equity=mktcap,
                    total_liabilities=o["tl"], sales=float(rev[last]), total_assets=o["ta"])
    zdec = VL.z_decomposition(o["ca"] - o["cl"], o["re"], _safe(inc, "经营利润", last),
                              mktcap, o["tl"], float(rev[last]), o["ta"])
    kdinfo = VL.cost_of_debt(rf, z)
    kd_implied = VL.implied_cost_of_debt(_safe(inc, "利息支出", last), o["std"] + o["cur"], o["ltd"])

    # ---------- FINC6021：可持续增长率交叉校验 ----------
    eqs = [_safe(bal, "归母权益", e) for e in ends]
    roe = (ni_all[-1] - nci[-1]) / np.mean(eqs[-2:]) if np.mean(eqs[-2:]) else np.nan
    g_sust = VL.sustainable_growth(payout, roe) if np.isfinite(roe) else np.nan
    g_ok, g_why = VL.sustainable_growth_is_meaningful(roe, o["eq"], o["ta"])

    # ---------- FINC6021：经营杠杆度 ----------
    fc_last = _safe(inc, "研发费用", last) + (sga_hist[-1] if np.isfinite(sga_hist[-1]) else 0) * rev[last]
    dol = VL.dol(fc_last, _safe(inc, "经营利润", last), taxr, _safe(cfs, "折旧摊销合计", last))

    # ---------- 估值基准日滚动（非 12-31 财年在这里体现）----------
    today = dt.date.today()
    fy_end = dt.date(int(last[:4]), int(last[5:7]), int(last[8:10]))
    roll = round((today - fy_end).days / 365.0, 3)

    fy_lab = lambda e: f"FY{e[:4]}A"
    proj_year = int(last[:4])
    return dict(
        meta=dict(ticker=meta["ticker"], name=meta["name"], cik=meta["cik"],
                  sic=meta["sic"], sic_desc=meta["sic_desc"], fye=meta["fye"],
                  price=q["price"], shares=round(shares, 4), mktcap=round(mktcap, 1),
                  quote_source=q["source"], asof=today.isoformat(), rf=rf, rf_date=rfd,
                  beta=beta5, beta_raw=raw5, beta_n=n5,
                  beta2=beta2, beta2_raw=raw2, beta2_n=n2,
                  nyears=nyears, last_annual=last, diag=st["diag"]),
        periods=dict(annual=ends, labels=[fy_lab(e) for e in ends],
                     proj=[f"FY{proj_year + i + 1}E" for i in range(nyears)]),
        stmts=dict(income=inc, balance=bal, cashflow=cfs, shares=shr),
        open=o,
        gen=dict(rd=round(rd, 5), sga=round(sga, 5), other_op=round(resid, 5),
                 tax=round(taxr, 4), mi_share=round(mi_share, 5),
                 dso=round(dso, 1), dio=round(dio, 1), dpo=round(dpo, 1),
                 ocap=round(ocap, 5), oclp=round(oclp, 5),
                 deprate=round(deprate, 4), amort=round(amort, 5),
                 sbc=round(sbc, 5), payout=round(payout, 4),
                 irate=round(kd_implied, 5) if np.isfinite(kd_implied) else round(kdinfo["kd"], 5),
                 cash_yield=round(min(rf, 0.045), 4),
                 mincash=round(max(o["cash"] * 0.5, float(rev[last]) * 0.03), 1),
                 sweep=round(float(rev[last]) * 0.05, 1)),
        judg=dict(growth=_fade(g_hist[-1] if g_hist else .05, .025, nyears,
                               fade_years if fade_years else min(nyears, 5)),
                  fade_years=int(fade_years) if fade_years else min(nyears, 5),
                  gm=[round(gm_mean, 4)] * nyears,
                  capex=[round(capex, 4)] * nyears,
                  sbc_path=[round(sbc, 5)] * nyears,
                  g_hist=[round(x, 4) for x in g_hist],
                  gm_hist=[round(x, 4) for x in gm_hist],
                  g_stats=(round(float(np.mean(g_hist)), 4), round(float(np.std(g_hist)), 4),
                           round(min(g_hist), 4), round(max(g_hist), 4)),
                  gm_stats=(round(gm_mean, 4), round(float(np.std(gm_hist)), 4),
                            round(min(gm_hist), 4), round(max(gm_hist), 4)),
                  buyback=round(buyback, 5)),
        dcf=dict(rf=rf, erp=(geo["erp"] if geo["ok"] else erp_i["erp"]), beta=beta5, kd=kdinfo["kd"], wd=round(wd, 4),
                 # 终值税率用【合并边际税率】而非联邦法定税率：
                 # 21%（联邦）+ 5%（州加权）×(1 − 21%) = 24.95% ≈ 25%。
                 # 州税可在联邦税前扣除，所以不是简单相加。用 21% 会让终值 NOPAT
                 # 高估 5.06%，而终值占 EV 约 67–75%。
                 g=.025, t_term=.25, midyear=1, roll=roll,
                 nonop=round(o["lti"], 1), mi_mode=2,
                 rd_cap=1, rd_life=_life, roic_mode=1),
        rdcap=dict(**rdcap, rd_hist=[round(_rd_all[k], 1) for k in sorted(_rd_all)][-8:],
                   ic_reported=round(ic["operating"], 1), ic_financing=round(ic["financing"], 1),
                   ic_gap=ic["gap"], ic_rdcap=round(o["ic_rdcap"], 1)),
        erp_info=erp_i, interim=itm, interim_skip=itm_skip, geo=geo,
        course=dict(z=kdinfo["z"], zone=kdinfo["zone"], rating=kdinfo["rating"],
                    z_terms=zdec.get("terms"), z_d_share=zdec.get("d_share"),
                    kd_wacc_bp=round(VL.wacc_sensitivity_to_kd(wd, taxr) * 10000, 2),
                    rating_note="合成评级仅作默认值。投资级顶端 Z 无分辨力（D 项占 "
                                f"{zdec.get('d_share', 0):.0%}），且实测系统性高估 1–2 个子级"
                                "（实际 S&P：MSFT AAA、AAPL AA+、GOOGL AA+、NVDA AA）。"
                                "已知实际评级时请直接改「信用评级」这一格。",
                    spread=kdinfo["spread"], spread_src=kdinfo["source"],
                    kd_implied=round(kd_implied, 5) if np.isfinite(kd_implied) else None,
                    roe=round(roe, 4) if np.isfinite(roe) else None,
                    g_sustainable=round(g_sust, 4) if np.isfinite(g_sust) else None,
                    g_sust_usable=g_ok, g_sust_note=g_why,
                    dol=round(dol, 3) if np.isfinite(dol) else None,
                    payout=round(payout, 4)),
        fin=dict(new_shares=0.0, issue_price=round(q["price"], 2), fee=.010,
                 year=1, use_capex=0.0, use_debt=0.0,
                 build_years=min(3, nyears), disc_years=0.0,
                 buyback_pct=round(buyback, 5)),
        _rho=round(_rho, 3),
        rev={e: float(rev[e]) for e in ends},
        # 各年营业成本：ir-agent 的 assume._base_margin_of 按年度从 rev / cogs 算基期毛利率
        cogs={e: float(_safe(inc, "营业成本", e)) for e in ends},
    )
