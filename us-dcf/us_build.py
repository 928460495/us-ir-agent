# -*- coding: utf-8 -*-
"""美股 DCF 模板 — 工作簿构建层。写全公式 Excel，单位百万美元。

本包完全自包含：不 import、不修改 ashare-dcf 的任何文件。

US GAAP 相对 CAS 的结构性差异（体现在本文件里）：
  · EBIT 直接取报表的 Operating Income —— 它本来就在利息之前，不必像 CAS 那样
    用「营业利润 + 财务费用」倒推。
  · 没有「营业税金及附加」「其他收益」；利息收支分列；SG&A 常合并列报。
  · SBC（股份支付）是美股特有的大额非现金费用，且与股数稀释强绑定。
  · 回购是常态（AAPL 五年回购额 ≈ 营收的 22%），股数必须逐年滚动。

SBC 口径（用户选定：按 Damodaran）：
  SBC 已含在报表的 R&D / SG&A 里，因此「什么都不做」＝ 已费用化 ＝ Damodaran 口径。
  与之配套的必然要求是【股数不再因 SBC 而摊薄】—— 既然现金流里已经罚过一次，
  再加进股数就是罚两次。模型把这两件事绑在同一个开关上，无法只做一半。

经营租赁（用户选定）：经营租赁负债【不计入】净负债。理由是 ASC 842 下经营租赁
  费用仍留在营业费用内（单一直线法），Operating Income 已经扣过租金；再把负债
  计入净负债就是重复扣一次。模型另给出租赁负债金额供参考。
"""
from __future__ import annotations
import numpy as np
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.chart import LineChart, BarChart, Reference, Series
from openpyxl.chart.title import Title
from openpyxl.chart.text import RichText, Text
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.marker import Marker
from openpyxl.chart.axis import ChartLines
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.chart.layout import Layout, ManualLayout
from openpyxl.drawing.line import LineProperties
from openpyxl.drawing.text import (CharacterProperties, Paragraph, ParagraphProperties,
                                   RegularTextRun)
from openpyxl.comments import Comment
from openpyxl.utils import get_column_letter as CL

NAVY = "1F3864"; RED = "C00000"; GREY = "808080"
F_TTL = Font(bold=True, size=14, color=NAVY)
F_HDR = Font(bold=True, size=11, color="FFFFFF")
F_COL = Font(bold=True, size=10, color="FFFFFF")
F_IN = Font(size=10, color="0000CD")                    # 蓝色 = 可编辑输入
F_FX = Font(size=10)                                    # 黑色 = 公式
F_LNK = Font(size=10, color="008000")                   # 绿色 = 跨表链接
F_H = Font(size=10, italic=True, color="404040")        # 斜体 = 历史静态
P_N = PatternFill("solid", fgColor=NAVY)
P_B = PatternFill("solid", fgColor="2E75B6")
P_G = PatternFill("solid", fgColor="F2F2F2")            # 浅灰 = 禁用态（配灰字）
P_H = PatternFill("solid", fgColor=NAVY)                # 深蓝 = 历史列表头（配白字）
# 表头一律【深色底 + 白字】。历史列曾用浅灰底配白字，等于看不见 ——
# 白字只能配深底，这条对所有表头统一适用。
P_Y = PatternFill("solid", fgColor="FFF2CC")            # 黄底 = 判断项
P_O = PatternFill("solid", fgColor="FCE4D6")            # 橙底 = 校验格
BX = Border(bottom=Side(style="thin", color="D9D9D9"))
NUM = "#,##0.0"; PCT = "0.0%"; PCT2 = "0.00%"; USD = '"$"#,##0.00'
MX = "0.00"; D1 = "0.0"; Z = "0.000000"


# 「1b_驱动分解」—— 自 ashare_build 原样移植（只换金额单位）：两个引擎同一张驱动表。
_SCEN_COLS = ("B", "C", "D")          # Bull / Base / Bear —— 与 1_Assumptions 一致


_PCT_UNITS = ("变化率", "%", "百分比")


def _build_driver_sheet(wb, drv, YE, assum_ws, g_row, m_row):
    """建「1b_驱动分解」，并把 1_Assumptions 的增速/毛利率改为跨表引用。

    **公式化（ir-agent 2026-10-14，drivers schema 2）。** ir-agent 交来模式、因子、
    常量、基期值、每档逐年取值，以及两条公式的**已校验记号序列**。这里**不做
    解析**，只把记号换成单元格引用（`_ref_tokens`）——解析只在 ir-agent 一处。

    布局：常量区 → 基期（各因子第 0 年、营收₀、毛利率₀）→ 每年：各因子三档取值
    （黄底可手改）、「营收（合成）」「营收增速」「毛利率（合成）」。1_Assumptions 的营收
    增速与毛利率引用「营收增速」「毛利率（合成）」两行。改一个因子或常量，合成格
    与下游估值随之重算。

    **列序必须与 1_Assumptions 一致（B=Bull C=Base D=Bear）**，它与 CHOOSE($B$SCEN,…)
    绑定。写反不会让验算失败 —— 公式本身没错，只是「选悲观」会选出乐观值。
    """
    ds = wb.create_sheet("1b_驱动分解", 2)
    ds.column_dimensions["A"].width = 34
    for c in "BCD": ds.column_dimensions[c].width = 12
    ds.column_dimensions["E"].width = 12
    ds.column_dimensions["F"].width = 40
    r = 1
    ds.cell(r, 1, f"驱动因子分解（{drv['mode']}模式）").font = Font(bold=True, size=12)
    r += 1
    for t in ("revenue", "gross_margin"):
        txt = (drv.get("formula_text") or {}).get(t)
        if txt:
            ds.cell(r, 1, txt).font = Font(size=9, color="808080"); r += 1
    r += 1
    for i, t in enumerate(["项目 / 年", "Bull", "Base", "Bear", "单位", "出处"]):
        c = ds.cell(r, i + 1, t); c.font = F_COL; c.fill = P_B
    r += 1

    factors = drv["factors"]
    const_row: dict[str, int] = {}
    for c in drv.get("constants") or []:
        ds.cell(r, 1, f"常量 {c['name']}").font = Font(size=10)
        cc = ds.cell(r, 2, c["value"]); cc.font = F_IN; cc.fill = P_Y
        ds.cell(r, 6, c.get("source", "")).font = Font(size=9, color="808080")
        const_row[c["name"]] = r; r += 1

    base = drv["base"]
    base_row: dict[str, int] = {}
    for f in factors:
        if f["short"] not in (base.get("factors") or {}):
            continue
        ds.cell(r, 1, f"基期 {f['label']}").font = Font(size=10)
        cc = ds.cell(r, 2, base["factors"][f["short"]]); cc.font = F_IN; cc.fill = P_Y
        cc.number_format = PCT if f["unit"] in _PCT_UNITS else "0.00"
        ds.cell(r, 5, f["unit"]).font = Font(size=9, color="808080")
        base_row[f["short"]] = r; r += 1

    def ref(tokens, yi, col, case, rows):
        """记号 → Excel 公式文本。yi 为第几年（0 起），-1 表示基期。"""
        out = []
        for kind, val in tokens:
            if kind in ("num", "op"):
                out.append(val)
            elif kind == "lp":
                out.append("(")
            elif kind == "rp":
                out.append(")")
            elif kind == "name":
                if val in const_row:
                    out.append(f"$B${const_row[val]}")
                elif yi < 0:
                    out.append(f"$B${base_row[val]}")
                else:
                    out.append(f"{col}{rows['f'][yi][val]}")
            elif kind == "prev":
                if yi < 0:
                    raise ValueError(f"第 0 年取不到 上期({val})")
                if val == "营收":
                    out.append(f"$B${rows['rev0']}" if yi == 0
                               else f"{col}{rows['rev'][yi - 1]}")
                elif val == "毛利率":
                    anc = drv.get("anchor_y1")
                    if yi == 0:
                        out.append(f"$B${rows['gm0']}")
                    elif yi == 1 and anc:
                        out.append(f"{anc[case]}")
                    else:
                        out.append(f"{col}{rows['gm'][yi - 1]}")
                else:
                    out.append(f"$B${base_row[val]}" if yi == 0
                               else f"{col}{rows['f'][yi - 1][val]}")
            elif kind == "base":
                if val == "营收":
                    out.append(f"$B${rows['rev0']}")
                elif val == "毛利率":
                    bls = drv.get("base_levels")
                    out.append(f"{bls[case][max(yi, 0)]}" if bls and yi >= 0
                               else f"$B${rows['gm0']}")
                else:
                    out.append(f"$B${base_row[val]}")
        return "".join(out)

    rows = {"f": [], "rev": [], "growth": [], "gm": []}
    fm = drv["formulas"]
    # 第 0 年的营收、毛利率：变化模式取账本（亿元），水平模式由基期因子代入公式。
    ds.cell(r, 1, "基期 营收（百万美元）").font = Font(size=10, bold=True)
    if drv["mode"] == "水平":
        ds.cell(r, 2, "=" + ref(fm["revenue"], -1, "B", "base", rows)).font = F_FX
    else:
        ds.cell(r, 2, base["revenue"]).font = F_IN
    ds.cell(r, 2).number_format = "#,##0.00"
    rows["rev0"] = r; r += 1
    ds.cell(r, 1, "基期 毛利率").font = Font(size=10, bold=True)
    if drv["mode"] == "水平":
        ds.cell(r, 2, "=" + ref(fm["gross_margin"], -1, "B", "base", rows)).font = F_FX
    else:
        ds.cell(r, 2, base["margin"]).font = F_IN
    ds.cell(r, 2).number_format = PCT
    rows["gm0"] = r; r += 2

    # 覆盖年数以 values 实际给出的档数为准，不强行对齐 YE 全长 —— 未覆盖的年份
    # 1_Assumptions 保留原直接取值，不被本表覆盖。
    n_cov = len(drv["values"]["base"])
    cases = ("bull", "base", "bear")
    for yi, y in enumerate(YE[:n_cov]):
        frow = {}
        for fi, f in enumerate(factors):
            ds.cell(r, 1, f"{y} {f['label']}").font = Font(size=10)
            for ci, case in enumerate(cases):
                cc = ds.cell(r, 2 + ci, drv["values"][case][yi][fi])
                cc.font = F_IN; cc.fill = P_Y
                cc.number_format = PCT if f["unit"] in _PCT_UNITS else "0.00"
            ds.cell(r, 5, f["unit"]).font = Font(size=9, color="808080")
            frow[f["short"]] = r; r += 1
        rows["f"].append(frow)
        for key, label, toks, fmt in (("rev", "营收（合成）", fm["revenue"], "#,##0.00"),
                                      ("gm", "毛利率（合成）", fm["gross_margin"], PCT)):
            ds.cell(r, 1, f"{y} {label}").font = Font(size=10, bold=True)
            for ci, col in enumerate(_SCEN_COLS):
                cc = ds.cell(r, 2 + ci, "=" + ref(toks, yi, col, cases[ci], rows))
                cc.font = F_FX; cc.number_format = fmt
            rows[key].append(r); r += 1
            if key == "rev":
                ds.cell(r, 1, f"{y} 营收增速（合成）").font = Font(size=10, bold=True)
                for ci, col in enumerate(_SCEN_COLS):
                    prev = (f"$B${rows['rev0']}" if yi == 0
                            else f"{col}{rows['rev'][yi - 1]}")
                    cc = ds.cell(r, 2 + ci, f"={col}{rows['rev'][yi]}/{prev}-1")
                    cc.font = F_FX; cc.number_format = PCT
                rows["growth"].append(r); r += 1
        r += 1

    # 1_Assumptions 的判断项改为引用本表 —— 只覆盖本表实际分解到的年份
    for start, key in ((g_row, "growth"), (m_row, "gm")):
        for yi, r_ in enumerate(rows[key]):
            for ci, col in enumerate(_SCEN_COLS):
                assum_ws.cell(start + yi, 2 + ci,
                              f"='1b_驱动分解'!{col}{r_}").font = F_FX


def _erp_note(cal: dict) -> str:
    """ERP 这一格的注释要把「为什么默认用隐含值」和「还有哪些口径」都摆出来。"""
    e = cal.get("erp_info") or {}
    alts = e.get("alts") or {}
    lab = {"T12m": "T12m（默认，他每月对外引用的口径）",
           "T12m_payout": "T12m + 可持续派息", "T12m_adj_rf": "T12m + 调整无风险利率",
           "normalized": "正常化", "smoothed": "平滑", "net_cash_yield": "净现金收益率"}
    lines = ["、".join(f"{lab.get(k, k)} {v:.2%}" for k, v in alts.items())] if alts else []
    return ("默认 = Damodaran 市场隐含 ERP，自动抓取（" + str(e.get("date", "?")) + "，"
            + str(e.get("source", "")) + "）。"
            "隐含 ERP 是【由市场当前价格反解出来】的折现率溢价；用高于它的值估个股，"
            "等于结构上保证「看什么都贵」—— 实测由 5.0% 降到隐含值，"
            "四只大盘科技股的低估幅度平均收窄约 10pp。"
            + ("　其他口径：" + lines[0] if lines else "")
            + "　可选：实务惯例 5.0%（更保守）；课程历史口径 6–8%。"
            "　注意：隐含 ERP 是相对他表内的 T.Bond Rate "
            + (f"{e.get('tbond'):.2%}" if isinstance(e.get("tbond"), (int, float)) else "?")
            + " 算出来的，本模型的无风险利率取自 FRED，两者可能差几个 bp")


def build(cal: dict, path: str) -> dict:
    M, PR, O, GEN, JU, DC = (cal["meta"], cal["periods"], cal["open"],
                             cal["gen"], cal["judg"], cal["dcf"])
    FIN, CO = cal["fin"], cal["course"]
    INC, BAL, CFS, SHR = (cal["stmts"]["income"], cal["stmts"]["balance"],
                          cal["stmts"]["cashflow"], cal["stmts"]["shares"])
    HL, YE = PR["labels"], PR["proj"]
    ENDS = PR["annual"]
    NH, NY = len(HL), len(YE)
    HC = [CL(2 + i) for i in range(NH)]
    CC = [CL(2 + NH + i) for i in range(NY)]
    BASE = HC[-1]
    A = "'1_Assumptions'"; P = "'2_Income'"; B_ = "'3_Balance'"; K = "'4_CashFlow'"
    wb = Workbook()

    def pc(c):
        i = CC.index(c)
        return BASE if i == 0 else CC[i - 1]

    def hv(df, col, i):
        """历史值。取不到返回 None —— 绝不静默填 0。"""
        if col not in df.columns:
            return None
        v = df.loc[ENDS[i], col]
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return None if not np.isfinite(v) else round(v, 2)

    # ================= 1_Assumptions =================
    ws = wb.active; ws.title = "1_Assumptions"; ws.sheet_properties.tabColor = NAVY
    ws.column_dimensions["A"].width = 46
    for i in range(2, 7):
        ws.column_dimensions[CL(i)].width = 14
    ws.column_dimensions["F"].width = 92
    ws.cell(1, 1, f"{M['name']} ({M['ticker']}) — DCF 假设页（百万美元，US GAAP）").font = F_TTL
    ws.cell(2, 1, f"SIC {M['sic']} {M['sic_desc']}　｜　财年末 {M['fye']}　｜　"
                  f"现价 ${M['price']:.2f}　｜　评估日 {M['asof']}").font = Font(size=9, italic=True, color=GREY)
    ws.cell(3, 1, "蓝色=可编辑输入　黑色=公式　绿色=跨表链接　黄底=判断项（模板不外推，请自行拉开）"
            ).font = Font(size=9, italic=True, color=RED)

    def sec(r, t):
        ws.cell(r, 1, t).font = F_HDR
        for c in range(1, 7):
            ws.cell(r, c).fill = P_N
        return r + 1

    def inp(r, lbl, v, fmt=PCT, note=None, editable=True):
        ws.cell(r, 1, lbl).font = Font(size=10); ws.cell(r, 1).border = BX
        cc = ws.cell(r, 2, None if v is None else (round(float(v), 6) if isinstance(v, (int, float)) else v))
        cc.font = F_IN if editable else F_FX; cc.number_format = fmt
        if note:
            ws.cell(r, 6, note).font = Font(size=9, color=GREY)
        return r + 1

    r = 5
    r = sec(r, "一、情景开关")
    SCEN = r
    ws.cell(r, 1, "选择情景  1=Bull / 2=Base / 3=Bear").font = Font(bold=True, size=10)
    c = ws.cell(r, 2, 2); c.font = F_IN; c.fill = P_O; c.number_format = "0"
    ws.cell(r, 6, "◀ 改这一格切换全模型情景").font = Font(size=10, color=RED, bold=True); r += 1
    ws.cell(r, 1, "当前情景").font = Font(bold=True, size=10)
    ws.cell(r, 2, f'=CHOOSE(B{SCEN},"Bull","Base","Bear")').font = F_LNK; r += 2

    r = sec(r, "二、判断项 —— 模板不做外推，三档初始同值，请自行拉开")
    for i, t in enumerate(["假设项", "Bull", "Base", "Bear", "生效值"]):
        cc = ws.cell(r, i + 1, t); cc.font = F_COL; cc.fill = P_B
        cc.alignment = Alignment(horizontal="center")
    ws.cell(r, 6, "历史参考").font = F_COL; ws.cell(r, 6).fill = P_B; r += 1
    gs, gms = JU["g_stats"], JU["gm_stats"]

    def band(r0, rows, vals, fmt, hist, scen=None):
        """scen：上游（ir-agent 的 assume.apply_to）给出的三档 {"bull","base","bear"}；
        没有时三档同值（模板原样，提示使用者自行拉开）。列序 B=Bull C=Base D=Bear。"""
        for i, y in enumerate(rows):
            ws.cell(r0 + i, 1, y).font = Font(size=10); ws.cell(r0 + i, 1).border = BX
            v3 = [scen[k][i] for k in ("bull", "base", "bear")] if scen else [vals[i]] * 3
            for j in range(3):
                cc = ws.cell(r0 + i, 2 + j, v3[j]); cc.font = F_IN
                cc.number_format = fmt; cc.fill = P_Y
            cc = ws.cell(r0 + i, 5, f"=CHOOSE($B${SCEN},B{r0+i},C{r0+i},D{r0+i})")
            cc.font = F_FX; cc.number_format = fmt
            if i == 0:
                ws.cell(r0 + i, 6, hist).font = Font(size=9, color=GREY)
        return r0 + len(rows)

    G_ROW = r
    r = band(r, [f"{y} 营收增速" for y in YE], JU["growth"], PCT,
             f"历史 {'／'.join(f'{x:.1%}' for x in JU['g_hist'])}｜均值 {gs[0]:.1%} 标准差 {gs[1]:.1%}",
             JU.get("growth_scen"))
    r += 1
    M_ROW = r
    r = band(r, [f"{y} 毛利率" for y in YE], JU["gm"], PCT,
             f"历史 {'／'.join(f'{x:.1%}' for x in JU['gm_hist'])}｜近3年均值 {gms[0]:.1%}",
             JU.get("gm_scen"))
    r += 1
    CX_ROW = r
    r = band(r, [f"{y} 资本开支 / 营收" for y in YE], JU["capex"], PCT,
             f"近3年均值 {JU['capex'][0]:.1%}")
    r += 1
    SB_ROW = r
    r = band(r, [f"{y} SBC / 营收" for y in YE], JU["sbc_path"], PCT2,
             f"近3年均值 {GEN['sbc']:.2%}｜美股特有的大额非现金费用")
    r += 1
    DG_ROW = r; r = inp(r, "全期营收增速 Δ（敏感性用）", 0.0, PCT, "加在每一年的生效增速上")
    DM_ROW = r; r = inp(r, "全期毛利率 Δ（敏感性用）", 0.0, PCT, "加在每一年的生效毛利率上")
    r += 1

    # ---------- 中期锚定 ----------
    # 年报期末可能已过去近一年，其后的季度都是【已成事实】。
    # 实测 AAPL：年报距今 349 天，已披露 3 个季度 = 基数年营收的 88%，
    # 模型却把那一年整个当成纯预测 —— 在已知事实面前继续猜。用上之后每股 +9.0%。
    IT = cal.get("interim")
    IT_ROWS = {}
    if IT and IT.get("fy_rev"):
        # 与 A 股同一规则（us_calibrate.quarterly_estimate）：已公布季度用实际，其余季度 =
        # 去年同季 ×（1 + 年初至今同比），营收与营业成本各用各的同比。占比法降为参考。
        k = int(IT["k"])
        r = sec(r, f"三、锚定已披露季度（{IT['first']} ~ {IT['last']}，{IT['label']}已确定："
                   "已公布季度用实际，其余季度按年初至今同比外推）")
        SW_LBL = "锚定开关（1=锚定 0=按增速外推）"
        ws.cell(r, 1, SW_LBL).font = Font(size=10); ws.cell(r, 1).border = BX
        cc = ws.cell(r, 2, 1); cc.font = F_IN; cc.fill = P_O; cc.number_format = "0"
        ws.cell(r, 6, "1 时首个预测年营收与毛利率取下方估算，忽略上方该年的判断项（三档相同：首年是实际 + 机械外推）"
                ).font = Font(size=9, color=GREY)
        IT_ROWS[SW_LBL] = r; r += 1

        def qrow(lbl, v, note=""):
            nonlocal r
            ws.cell(r, 1, lbl).font = Font(size=10); ws.cell(r, 1).border = BX
            c = ws.cell(r, 2, round(float(v), 2)); c.font = F_LNK; c.number_format = NUM
            if note:
                ws.cell(r, 6, note).font = Font(size=9, color=GREY)
            r += 1
            return r - 1

        P = {}
        for tag, name in (("rev", "营业收入"), ("cogs", "营业成本")):
            P[f"prev_{tag}"] = [qrow(f"去年第{i + 1}季{name}", v,
                                     "全年 − 前三季（公司不为第四季度单报 10-Q）" if i == 3 else "取自 10-Q")
                                for i, v in enumerate(IT[f"q_prev_{tag}"])]
            P[f"cur_{tag}"] = [qrow(f"本年第{i + 1}季{name}", v, "已公布，取自 10-Q")
                               for i, v in enumerate(IT[f"q_cur_{tag}"])]
        for tag, name in (("rev", "营业收入"), ("cogs", "营业成本")):
            cur, prev = P[f"cur_{tag}"], P[f"prev_{tag}"]
            ws.cell(r, 1, f"{name}年初至今同比").font = Font(size=10)
            c = ws.cell(r, 2, f"=({'+'.join(f'B{x}' for x in cur)})/({'+'.join(f'B{x}' for x in prev[:k])})-1")
            c.font = F_FX; c.number_format = PCT2
            IT_ROWS[f"yoy_{tag}"] = r; r += 1
        for tag, name in (("rev", "营业收入"), ("cogs", "营业成本")):
            cur, prev = P[f"cur_{tag}"], P[f"prev_{tag}"]
            ws.cell(r, 1, f"→ 隐含全年{name}").font = Font(bold=True, size=10)
            c = ws.cell(r, 2, f"=({'+'.join(f'B{x}' for x in cur)})"
                              f"+({'+'.join(f'B{x}' for x in prev[k:])})*(1+B{IT_ROWS[f'yoy_{tag}']})")
            c.font = Font(bold=True, size=10); c.number_format = NUM
            IT_ROWS[f"fy_{tag}"] = r; r += 1
        if IT.get("implied_fy"):
            ws.cell(r, 1, "　〔参考〕占比法隐含全年营业收入").font = Font(size=10)
            c = ws.cell(r, 2, round(float(IT["implied_fy"]), 2)); c.font = F_H; c.number_format = NUM
            ws.cell(r, 6, f"已披露 ÷ 近年同期占比均值 {IT['share']*100:.2f}%（σ {IT['share_std']*100:.2f}pp）。"
                          "不进生效值：对周期股与高速增长股会失灵").font = Font(size=9, color=GREY)
            r += 1
        r += 1
    elif IT:
        r = sec(r, "三、锚定已披露季度 —— 本次不锚定，首个预测年按上方营收增速判断项外推")
        ws.cell(r, 1, f"原因：{cal.get('interim_skip') or '季度数据不足'}").font = Font(size=10, color=RED)
        r += 2

    r = sec(r, "四、自动校准假设（从历史推导；可覆盖）")
    GEN_SPEC = [
        ("研发费用 / 营收", GEN["rd"], PCT, "近3年均值｜US GAAP 全额费用化（不含可资本化软件成本）"),
        ("销售及管理费用 / 营收", GEN["sga"], PCT, "近3年均值｜标签缺失时由「销售费用+管理费用」相加"),
        ("其他经营损益 / 营收", GEN["other_op"], PCT2,
         "残差项：令重建的经营利润【精确】等于报表 Operating Income。"
         "重组、减值、诉讼和解等未被上面几项捕捉的科目都落在这里，不会凭空消失"),
        ("有效所得税率", GEN["tax"], PCT, "近3年 所得税/税前利润，夹于 5%–35%"),
        ("少数股东损益占比", GEN["mi_share"], PCT, "最近年报实际"),
        ("其他流动资产 / 营收", GEN["ocap"], PCT, "近3年均值"),
        ("其他流动负债 / 营收", GEN["oclp"], PCT, "近3年均值"),
        ("应收周转天数 DSO", GEN["dso"], D1, "应收 / 营收 × 365"),
        ("存货周转天数 DIO", GEN["dio"], D1, "存货 / 营业成本 × 365"),
        ("应付周转天数 DPO", GEN["dpo"], D1, "应付 / 营业成本 × 365"),
        ("折旧率（占期初固定资产净额）", GEN["deprate"], PCT,
         "分子用纯 PP&E 折旧（XBRL Depreciation），不含无形摊销 —— 与分母口径一致"),
        ("无形摊销 / 营收", GEN["amort"], PCT2, "无形资产与融资租赁摊销，单独按营收比例建模"),
        ("有息负债利率", GEN["irate"], PCT, "利息费用 ÷ 期初有息负债；缺披露时退回合成评级 kd"),
        ("现金收益率", GEN["cash_yield"], PCT, "现金与短期投资的收益率"),
        ("派息率（占归母净利）", GEN["payout"], PCT, "近3年实际"),
        ("最低现金余额", GEN["mincash"], NUM, "低于此触发循环贷"),
        ("现金归集阈值", GEN["sweep"], NUM, "超出最低现金此数则还短贷"),
    ]
    GEN_ROW = r
    for lbl, v, f, note in GEN_SPEC:
        r = inp(r, lbl, v, f, note)
    GR = {s[0]: GEN_ROW + i for i, s in enumerate(GEN_SPEC)}
    r += 1

    r = sec(r, f"五、{HL[-1]} 期初余额（模型起点，来自 SEC EDGAR 实际申报）")
    OP = [("现金及等价物", O["cash"]), ("短期投资", O["sti"]), ("应收账款", O["ar"]),
          ("存货", O["inv"]), ("其他流动资产", O["oca"]), ("固定资产净额", O["ppe"]),
          ("长期投资", O["lti"]), ("商誉与无形资产", O["gw"] + O["intan"]),
          ("其他非流动资产", O["onca"]),
          ("应付账款", O["ap"]), ("其他流动负债", O["ocl"]), ("一年内到期债务", O["cur"]),
          ("短期借款", O["std"]), ("长期债务", O["ltd"]), ("其他非流动负债", O["oncl"]),
          ("少数股东权益", O["mi"]), ("夹层权益", O.get("mezz", 0.0)), ("归母权益", O["eq"]),
          ("期初研发资本（Damodaran）", O.get("rd_asset", 0.0)),
          ("基期营业收入", cal["rev"][ENDS[-1]]),
          ("摊薄股数（百万股）", M["shares"]), ("现价（美元）", M["price"])]
    OPEN_ROW = r
    for lbl, v in OP:
        ws.cell(r, 1, lbl).font = Font(size=10); ws.cell(r, 1).border = BX
        cc = ws.cell(r, 2, float(v)); cc.font = F_IN
        cc.number_format = USD if "现价" in lbl else NUM
        r += 1
    OR_ = {s[0]: OPEN_ROW + i for i, s in enumerate(OP)}
    ws.cell(r, 1, "★ 期初平衡校验 = 资产 −（负债+权益）").font = Font(bold=True, size=10)
    cc = ws.cell(r, 2, f"=SUM(B{OR_['现金及等价物']}:B{OR_['其他非流动资产']})"
                       f"-SUM(B{OR_['应付账款']}:B{OR_['归母权益']})")
    cc.font = F_FX; cc.number_format = Z; cc.fill = P_O
    ws.cell(r, 6, "必须为 0").font = Font(size=9, color="006400"); r += 1
    ws.cell(r, 1, "　（备查）经营租赁负债 — 非流动").font = Font(size=10, color=GREY)
    cc = ws.cell(r, 2, float(O["lease_nc"])); cc.font = Font(size=10, color=GREY); cc.number_format = NUM
    ws.cell(r, 6, "ASC 842 下经营租赁费用仍在营业费用内，Operating Income 已扣过租金；"
                  "故【不】计入净负债，否则重复扣一次。此处仅备查").font = Font(size=9, color=GREY)
    r += 2

    r = sec(r, "六、DCF 参数")
    DP = [("无风险利率 Rf（10Y UST）", DC["rf"], PCT2, f"FRED DGS10，{M['rf_date']}"),
          ("股权风险溢价 ERP", DC["erp"], PCT2, _erp_note(cal)),
          ("Beta（5年对标普500回归+Blume）", DC["beta"], "0.000",
           f"原始 {M['beta_raw']}，样本 {M['beta_n']} 日｜2年窗口 {M['beta2_raw']}→{M['beta2']}（{M['beta2_n']} 日）。"
           "课程：5年平滑异常，2年更适合动态高成长或刚重组的公司 —— 窗口长度是判断项"),
          ("股权成本 Ke", None, PCT2, "Ke = Rf + Beta × ERP"),
          ("信用评级（可覆盖）", CO["rating"], "@", CO["rating_note"]),
          ("信用利差", CO["spread"], PCT2, CO["spread_src"]),
          ("债务成本 Kd（税前）", None, PCT2, "Kd = Rf + 信用利差（课程 Ch.4 公式）"),
          ("债务权重 D/(D+E)", DC["wd"], PCT2, "有息负债 ÷（有息负债 + 现价×股数）"),
          ("WACC", None, PCT2, "WACC = (1−Wd)×Ke + Wd×Kd×(1−t)"),
          ("永续增长率 g", DC["g"], PCT2,
           "Damodaran 规则：g 不应超过无风险利率（当前 Rf 即上限）；美国长期名义 GDP 增速约 4%"),
          ("终值所得税率 t_term", DC["t_term"], PCT2,
           "【合并边际税率】21%（联邦）+ 5%（州加权）×(1−21%) = 24.95% ≈ 25%。"
           "州税可在联邦税前扣除，所以不是简单相加。只填 21% 会让终值 NOPAT 高估 5.06%，"
           "而终值占 EV 约 67–75%。永续期不应假设税收优惠永存 —— "
           "明细期仍用历史有效税率（见上方自动校准）"),
          ("期中折现开关（1=是 0=否）", DC["midyear"], "0", "现金流全年均匀发生时取 1"),
          ("估值基准日滚动年数", DC["roll"], "0.000", f"{ENDS[-1]} → {M['asof']}（非 12-31 财年在此体现）"),
          ("非经营资产（加回）", DC["nonop"], NUM, "长期有价证券与权益法投资；短期投资已在净负债中扣除"),
          ("少数股东扣除方式（1=账面 2=价值）", DC["mi_mode"], "0", "价值法按其利润分成折算"),
          ("研发资本化开关（1=开 0=关）", DC.get("rd_cap", 1), "0",
           "Damodaran 口径：研发是投资不是费用。开启后【只影响投入资本与 ROIC】，"
           "不改 FCFF —— 明确期现金流仍与申报报表一一对应（可审计），"
           "而终值的 g = 再投资率 × ROIC 才有意义。"
           "关掉会怎样：AAPL 的 ROIC 从 82% 跳到 272%（回购把账面权益冲小所致），"
           "再投资率随之接近 0，终值等于把当前超额回报永续化"),
          ("研发摊销年限（年）", DC.get("rd_life", 5), "0",
           f"按 SIC {M['sic']} 自动映射（软件/互联网 3 年、半导体与硬件 5 年、制药 10 年）"),
          ("终值 ROIC 处理（1=沿用末年 2=收敛至 WACC+溢价）", DC.get("roic_mode", 1), "0",
           "沿用末年 = 假设当前超额回报永续，对高 ROIC 公司过于乐观；"
           "收敛 = 竞争终将侵蚀超额回报（Damodaran 更倾向此法）"),
          ("终值 ROIC 收敛溢价（模式2 用）", 0.03, PCT2,
           "收敛后的终值 ROIC = WACC + 本溢价。3% 代表仍保有温和护城河"),
          ("SBC 加回开关（0=费用化 1=加回）", 0, "0",
           "0 = Damodaran 口径（默认）：SBC 已含在 R&D/SG&A 内，不加回，且【股数不因 SBC 摊薄】。"
           "1 = 卖方惯例：加回 SBC，同时股数按 SBC/股价 摊薄。两件事绑在同一开关上，无法只做一半"),
          ]
    DCF_ROW = r
    for lbl, v, f, note in DP:
        ws.cell(r, 1, lbl).font = Font(size=10); ws.cell(r, 1).border = BX
        cc = ws.cell(r, 2, None if v is None else (v if isinstance(v, str) else round(float(v), 6)))
        cc.font = F_FX if v is None else F_IN; cc.number_format = f
        ws.cell(r, 6, note).font = Font(size=9, color=GREY); r += 1
    DR = {s[0]: DCF_ROW + i for i, s in enumerate(DP)}
    # ---------- 收入地域加权 ERP ----------
    # AAPL 国际收入 57%、GOOGL 52%，全按美国 ERP 折现等于假设在中国和在美国赚的钱风险一样。
    # 做成【活公式 + 可覆盖的权重/CRP 明细】，而不是一个静态数字 —— 映射本身是判断项，必须可审计。
    GEO = cal.get("geo") or {}
    if GEO.get("ok") and GEO.get("rows"):
        r += 1
        ws.cell(r, 1, "　〔ERP 明细〕收入地域加权（Damodaran 国别风险溢价）").font = Font(bold=True, size=10)
        ws.cell(r, 6, f"口径：成熟市场基准 = 标普隐含 ERP − 美国 CRP；"
                      f"加权 ERP = 基准 + Σ(收入权重 × 国别CRP)。纯美国收入时自动还原为标普隐含 ERP。"
                      f"分地域收入取自 FMP FY{GEO.get('fy','?')}").font = Font(size=9, color=GREY)
        r += 1
        base_row = r
        ws.cell(r, 1, "　　成熟市场基准（标普隐含 ERP − 美国 CRP）").font = Font(size=10)
        cc = ws.cell(r, 2, GEO["base"]); cc.font = F_LNK; cc.number_format = PCT2; r += 1
        ws.cell(r, 1, "　　地域"); ws.cell(r, 2, "收入权重"); ws.cell(r, 3, "映射到"); ws.cell(r, 4, "国别CRP")
        for c_ in range(1, 5):
            ws.cell(r, c_).font = F_COL; ws.cell(r, c_).fill = P_B
        r += 1
        g0 = r
        for row_ in GEO["rows"]:
            ws.cell(r, 1, "　　" + str(row_["segment"])[:38]).font = Font(size=10)
            cc = ws.cell(r, 2, round(row_["weight"], 4)); cc.font = F_IN; cc.number_format = PCT; cc.fill = P_Y
            ws.cell(r, 3, str(row_["mapped"])).font = Font(size=9, color=GREY)
            cc = ws.cell(r, 4, row_["crp"]); cc.font = F_IN; cc.number_format = PCT2; cc.fill = P_Y
            r += 1
        g1 = r - 1
        ws.cell(r, 1, "　　加权 CRP").font = Font(bold=True, size=10)
        # 写成显式乘加而非 SUMPRODUCT：验证器的单元格解析器要能算它，
        # 而 SUMPRODUCT 的区间语法（: 与 ,）超出了那个小解析器的范围。
        cc = ws.cell(r, 2, "=" + "+".join(f"B{i}*D{i}" for i in range(g0, g1 + 1)))
        cc.font = Font(bold=True, size=10); cc.number_format = PCT2
        wcrp_row = r
        ws.cell(r, 6, "权重与 CRP 都可改；映射不确定时直接改右侧 CRP 即可").font = Font(size=9, color=GREY)
        r += 1
        ws.cell(r, 1, "　★ 权重合计（应为 100%）").font = Font(size=10)
        cc = ws.cell(r, 2, f"=SUM(B{g0}:B{g1})"); cc.font = F_FX; cc.number_format = PCT; cc.fill = P_O
        r += 2
        # ERP 这一格改成活公式：改上面的权重/CRP，WACC 全链路自动跟着走
        ws.cell(DR["股权风险溢价 ERP"], 2).value = f"=B{base_row}+B{wcrp_row}"
        ws.cell(DR["股权风险溢价 ERP"], 2).font = F_FX
    KE, KD, WC_ = DR["股权成本 Ke"], DR["债务成本 Kd（税前）"], DR["WACC"]
    ws.cell(KE, 2).value = (f"=B{DR['无风险利率 Rf（10Y UST）']}"
                            f"+B{DR['Beta（5年对标普500回归+Blume）']}*B{DR['股权风险溢价 ERP']}")
    ws.cell(KD, 2).value = f"=B{DR['无风险利率 Rf（10Y UST）']}+B{DR['信用利差']}"
    ws.cell(WC_, 2).value = (f"=(1-B{DR['债务权重 D/(D+E)']})*B{KE}+B{DR['债务权重 D/(D+E)']}"
                             f"*B{KD}*(1-B{GR['有效所得税率']})")
    r += 1

    # ---------- 六、股本与融资 ----------
    r = sec(r, "七、股本与融资（回购 / 增发）")
    FN_SPEC = [
        ("年度回购金额 / 营收", FIN["buyback_pct"], PCT2,
         f"近3年实际 {FIN['buyback_pct']:.2%}。美股回购是常态，股数必须逐年滚动"),
        ("回购均价（美元/股）", M["price"], USD,
         "默认取现价。若现价高于内在价值，回购会【降低】每股内在价值 —— 模型会如实显示"),
        ("增发股数（百万股）", FIN["new_shares"], D1, "0 = 关闭增发。可为负（等同注销）"),
        ("增发价（美元/股）", FIN["issue_price"], USD, None),
        ("增发费用率", FIN["fee"], PCT, None),
        ("增发到账年份序号（1=首个预测年）", FIN["year"], "0", f"取 1..{NY}"),
        ("募资投向 — 资本开支", FIN["use_capex"], NUM, "唯一会改变经营现金流的一项"),
        ("募资净额（公式）", None, NUM, None),
        ("桥接折现年数", FIN["disc_years"], "0.00", "0 = 视同已完成"),
    ]
    FIN_ROW = r
    for lbl, v, f, note in FN_SPEC:
        ws.cell(r, 1, lbl).font = Font(size=10); ws.cell(r, 1).border = BX
        cc = ws.cell(r, 2, None if v is None else round(float(v), 6))
        cc.font = F_FX if v is None else F_IN; cc.number_format = f
        if note:
            ws.cell(r, 6, note).font = Font(size=9, color=GREY)
        r += 1
    FN = {s[0]: FIN_ROW + i for i, s in enumerate(FN_SPEC)}
    ws.cell(FN["募资净额（公式）"], 2).value = (
        f"=B{FN['增发股数（百万股）']}*B{FN['增发价（美元/股）']}*(1-B{FN['增发费用率']})")
    r += 1

    # ---------- 七、FINC6021 诊断 ----------
    r = sec(r, "八、方法诊断（FINC6021 · Titman & Martin）")
    DIAG = []

    def dg(lbl, val, fmt=MX, note=None):
        nonlocal r
        ws.cell(r, 1, lbl).font = Font(size=10); ws.cell(r, 1).border = BX
        cc = ws.cell(r, 2, val); cc.font = F_FX; cc.number_format = fmt
        if note:
            ws.cell(r, 6, note).font = Font(size=9, color=GREY)
        DIAG.append(r); r += 1

    dg("Altman Z-score", CO["z"], MX, f"判定：{CO['zone']}（>2.99 安全 / 1.81–2.99 灰色 / <1.81 困境）")
    dg("　其中 D 项（股权市值/总负债）占比", CO["z_d_share"], PCT,
       "占比越高，Z 越是「这家公司很值钱」的同义反复，越没有信用信息量。"
       "Altman 1968 年标定于制造业样本，彼时市值与负债同量级")
    dg("kd 变动 77bp 时 WACC 的变动（bp）", CO["kd_wacc_bp"], MX,
       "杠杆极低时这个数会小到无关紧要 —— 合成评级值不值得较真，取决于 Wd 而非 Z 算得多精")
    dg("ROE（归母 / 平均净资产）", CO["roe"], PCT)
    dg("可持续增长率 g =（1−派息率）× ROE", CO["g_sustainable"], PCT,
       ("✅ 可用作终值 g 的交叉校验" if CO["g_sust_usable"]
        else f"⚠ 不可用：{CO['g_sust_note']}。此类公司应以 ROIC 为锚（即本模型终值用的 g = 再投资率 × ROIC）"))
    dg("经营杠杆度 DOL = 1 + [FC(1−t) − Dep×t] / OCF", CO["dol"], MX,
       "固定成本占比越高，营业利润对收入越敏感，风险越高 → 理应用更低的倍数。"
       "课程两栋写字楼案例：naive 套倍数会高估 7%")
    ws.cell(r, 1, "★ Plug（财务政策变量）").font = Font(bold=True, size=10, color=RED)
    ws.cell(r, 2, "现金 + 循环贷").font = Font(size=10, color=RED)
    ws.cell(r, 6, "课程 Ch.6：plug 是财务模型中最重要的财务政策变量，不是技术细节。"
                  "本模型以现金为 plug、辅以循环贷；这意味着预测期的资本结构会【漂移】，"
                  "而 WACC 假定它恒定 —— 漂移量见「5_DCF」页的资本结构漂移诊断").font = Font(size=9, color=RED)
    r += 2

    ws.freeze_panes = "A5"
    if IT_ROWS.get("fy_rev"):
        # 首年「生效值」内置锚定分支：显示【真正生效】的增速/毛利率，而不是被忽略的输入。
        # 只引用假设页内的单元格，不引用利润表 —— 否则会造成结构性循环引用。
        SW = IT_ROWS["锚定开关（1=锚定 0=按增速外推）"]; BR = OR_["基期营业收入"]
        ws.cell(G_ROW, 5).value = (f"=IF($B${SW}=1,B{IT_ROWS['fy_rev']}/$B${BR}-1,"
                                   f"CHOOSE($B${SCEN},B{G_ROW},C{G_ROW},D{G_ROW})+$B${DG_ROW})")
        ws.cell(M_ROW, 5).value = (f"=IF($B${SW}=1,1-B{IT_ROWS['fy_cogs']}/B{IT_ROWS['fy_rev']},"
                                   f"CHOOSE($B${SCEN},B{M_ROW},C{M_ROW},D{M_ROW})+$B${DM_ROW})")
        for rr in (G_ROW, M_ROW):
            for cc_ in (2, 3, 4):
                ws.cell(rr, cc_).fill = P_G
                ws.cell(rr, cc_).font = Font(color=GREY, size=10, italic=True)
            ws.cell(rr, 6, "⚠ 锚定开启时本行输入不生效 —— 生效值由已披露季度反推（见上方第三节）"
                    ).font = Font(size=9, color=RED)
    cal["_rows"] = dict(SCEN=SCEN, G_ROW=G_ROW, M_ROW=M_ROW, CX_ROW=CX_ROW, SB_ROW=SB_ROW, IT=IT_ROWS,
                        GR=GR, OR_=OR_, DR=DR, FN=FN, DG=DG_ROW, DM=DM_ROW,
                        HC=HC, CC=CC, BASE=BASE, NH=NH, NY=NY, ENDS=ENDS)
    # 驱动因子分解：只有上游（ir-agent 的方法论预研）给出 drivers 时才建表；
    # 没有时工作簿形状与从前完全一致（与 ashare_build 同一约定）
    if JU.get("drivers"):
        _build_driver_sheet(wb, JU["drivers"], YE, ws, G_ROW, M_ROW)
    return _statements(wb, cal, path)


# ============================================================
# 2/3/4 三表
# ============================================================
def _statements(wb, cal, path):
    M, PR, O, GEN, JU, DC = (cal["meta"], cal["periods"], cal["open"],
                             cal["gen"], cal["judg"], cal["dcf"])
    INC, BAL, CFS, SHR = (cal["stmts"]["income"], cal["stmts"]["balance"],
                          cal["stmts"]["cashflow"], cal["stmts"]["shares"])
    R = cal["_rows"]
    HC, CC, BASE, NH, NY, ENDS = R["HC"], R["CC"], R["BASE"], R["NH"], R["NY"], R["ENDS"]
    GR, OR_, DR, FN = R["GR"], R["OR_"], R["DR"], R["FN"]
    G_ROW, M_ROW, CX_ROW, SB_ROW = R["G_ROW"], R["M_ROW"], R["CX_ROW"], R["SB_ROW"]
    HL, YE = PR["labels"], PR["proj"]
    A = "'1_Assumptions'"; P = "'2_Income'"; B_ = "'3_Balance'"; K = "'4_CashFlow'"

    def pc(c):
        i = CC.index(c)
        return BASE if i == 0 else CC[i - 1]

    def hv(df, col, i):
        if col not in df.columns:
            return None
        v = df.loc[ENDS[i], col]
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return None if not np.isfinite(v) else round(v, 2)

    def hrow(df, col):
        return [hv(df, col, i) for i in range(NH)]

    def sheet(title, tab):
        w = wb.create_sheet(title); w.sheet_properties.tabColor = tab
        w.column_dimensions["A"].width = 40
        for i in range(2, 3 + NH + NY):
            w.column_dimensions[CL(i)].width = 13
        w.cell(1, 1, f"{M['name']} ({M['ticker']}) — {title[2:]}（百万美元，US GAAP）").font = F_TTL
        w.cell(2, 1, f"{HC[0]}:{HC[-1]} = 历史实际（斜体静态，来自 SEC EDGAR）　｜　"
                     f"{CC[0]}:{CC[-1]} = 公式预测　｜　基数 = {HL[-1]}（{BASE} 列）"
               ).font = Font(size=9, italic=True, color=RED)
        w.cell(4, 1, title[2:]).font = F_HDR
        for c in range(1, 2 + NH + NY):
            w.cell(4, c).fill = P_N
        w.cell(5, 1, "科目（百万美元）").font = F_COL; w.cell(5, 1).fill = P_B
        for i, y in enumerate(HL + YE):
            cc = w.cell(5, 2 + i, y); cc.font = F_COL
            cc.alignment = Alignment(horizontal="center")
            cc.fill = P_H if i < NH else P_B
        w.freeze_panes = "B6"
        return w

    def emit(w, row, label, hist, fx, fmt=NUM, bold=False, note=None, fill=None):
        c0 = w.cell(row, 1, label); c0.font = Font(bold=bold, size=10); c0.border = BX
        if note:
            c0.comment = Comment(note, "Template")
        for i in range(NH):
            cc = w.cell(row, 2 + i, hist[i] if hist else None)
            cc.number_format = fmt; cc.font = F_H
        for i, c in enumerate(CC):
            cc = w.cell(row, 2 + NH + i, fx(c, i)); cc.number_format = fmt
            cc.font = Font(bold=True, size=10) if bold else F_FX
            if fill:
                cc.fill = fill
        return row + 1

    # ---------- 2_Income（行号固定 6..26）----------
    wi = sheet("2_Income", "2E75B6")
    (RV, RG, RC, RGP, RGM, RRD, RSG, ROO, REBIT, RIE, RII, RON,
     RPBT, RTAX, RNI, RMI, RNIP, RSH, REPS, RPE, RSBC) = range(6, 27)
    revh = hrow(INC, "营业收入")
    emit(wi, RV, "Revenue 营业收入", revh,
         lambda c, i: f"={pc(c)}{RV}*(1+{A}!$E${G_ROW+i}+{A}!$B${R['DG']})", NUM, True)
    gh = [None] + [round(revh[i] / revh[i-1] - 1, 6) if revh[i] and revh[i-1] else None
                   for i in range(1, NH)]
    emit(wi, RG, "  营收增速", gh, lambda c, i: f"={c}{RV}/{pc(c)}{RV}-1", PCT)
    cogsh = hrow(INC, "营业成本")
    emit(wi, RC, "Cost of revenue 营业成本", cogsh,
         lambda c, i: f"={c}{RV}*(1-{A}!$E${M_ROW+i}-{A}!$B${R['DM']})")
    emit(wi, RGP, "Gross profit 毛利",
         [round(revh[i] - cogsh[i], 2) if revh[i] is not None and cogsh[i] is not None else None
          for i in range(NH)], lambda c, i: f"={c}{RV}-{c}{RC}", NUM, True)
    emit(wi, RGM, "  毛利率",
         [round(1 - cogsh[i] / revh[i], 6) if revh[i] and cogsh[i] is not None else None
          for i in range(NH)], lambda c, i: f"={c}{RGP}/{c}{RV}", PCT)
    emit(wi, RRD, "R&D 研发费用", hrow(INC, "研发费用"),
         lambda c, i: f"={c}{RV}*{A}!$B${GR['研发费用 / 营收']}")
    sgah = []
    for i in range(NH):
        v = hv(INC, "销售及管理费用", i)
        if v is None:
            a_, b_ = hv(INC, "销售费用", i), hv(INC, "管理费用", i)
            v = None if (a_ is None and b_ is None) else (a_ or 0) + (b_ or 0)
        sgah.append(v)
    emit(wi, RSG, "SG&A 销售及管理费用", sgah,
         lambda c, i: f"={c}{RV}*{A}!$B${GR['销售及管理费用 / 营收']}")
    emit(wi, ROO, "Other operating 其他经营损益", [None] * NH,
         lambda c, i: f"={c}{RV}*{A}!$B${GR['其他经营损益 / 营收']}", NUM, False,
         "残差项：令重建的经营利润精确等于报表 Operating Income")
    emit(wi, REBIT, "Operating income (EBIT) 经营利润", hrow(INC, "经营利润"),
         lambda c, i: f"={c}{RGP}-{c}{RRD}-{c}{RSG}+{c}{ROO}", NUM, True,
         "US GAAP 的 Operating Income 本来就在利息之前，直接就是 EBIT，"
         "不必像 CAS 那样用「营业利润 + 财务费用」倒推")
    emit(wi, RIE, "Interest expense 利息支出", hrow(INC, "利息支出"),
         lambda c, i: f"=({B_}!{pc(c)}18+{B_}!{pc(c)}19+{B_}!{pc(c)}21)*{A}!$B${GR['有息负债利率']}",
         NUM, False, "期初有息负债 × 利率（用期初值，避免循环引用）")
    emit(wi, RII, "Interest & investment income 利息与投资收益", hrow(INC, "利息及投资收益"),
         lambda c, i: f"=({B_}!{pc(c)}6+{B_}!{pc(c)}7)*{A}!$B${GR['现金收益率']}")
    emit(wi, RON, "Other non-operating 其他非经营损益", hrow(INC, "其他损益净额"),
         lambda c, i: "=0", NUM, False, "预测期按 0 —— 非经常项目不外推")
    emit(wi, RPBT, "Pretax income 税前利润", hrow(INC, "税前利润"),
         lambda c, i: f"={c}{REBIT}-{c}{RIE}+{c}{RII}+{c}{RON}", NUM, True)
    emit(wi, RTAX, "Income tax 所得税", hrow(INC, "所得税"),
         lambda c, i: f"={c}{RPBT}*{A}!$B${GR['有效所得税率']}")
    emit(wi, RNI, "Net income 净利润", hrow(INC, "净利润"),
         lambda c, i: f"={c}{RPBT}-{c}{RTAX}", NUM, True)
    emit(wi, RMI, "  less: NCI 少数股东损益", hrow(INC, "少数股东损益"),
         lambda c, i: f"={c}{RNI}*{A}!$B${GR['少数股东损益占比']}")
    emit(wi, RNIP, "Net income to parent 归母净利润", hrow(INC, "归母净利润"),
         lambda c, i: f"={c}{RNI}-{c}{RMI}", NUM, True)
    emit(wi, RSH, "Diluted shares 摊薄股数（百万股）", hrow(SHR, "摊薄股数"),
         lambda c, i: f"={K}!{c}30", NUM, False,
         "逐年滚动：期初 − 回购股数 + SBC股数(仅在加回口径下) + 增发股数")
    emit(wi, REPS, "Diluted EPS", [None] * NH, lambda c, i: f"={c}{RNIP}/{c}{RSH}", USD, True)
    emit(wi, RPE, "  P/E（按现价）", [None] * NH,
         lambda c, i: f"={A}!$B${OR_['现价（美元）']}/{c}{REPS}", MX)
    emit(wi, RSBC, "（备查）SBC 股份支付", hrow(CFS, "股份支付"),
         lambda c, i: f"={c}{RV}*{A}!$E${SB_ROW+i}", NUM, False,
         "已含在上方 R&D / SG&A 内。默认口径不加回（Damodaran），故股数也不因 SBC 摊薄")

    # ---------- 3_Balance（行号固定 6..30）----------
    wb_ = sheet("3_Balance", "2E75B6")

    def eb(row, label, hcol, fx, fmt=NUM, bold=False, note=None, fill=None, hist=None):
        h = hist if hist is not None else (hrow(BAL, hcol) if hcol else [None] * NH)
        emit(wb_, row, label, h, fx, fmt, bold, note, fill)

    eb(6, "Cash & equivalents 现金", "现金及等价物", lambda c, i: f"={K}!{c}27",
       NUM, False, "模型 plug = 现金流量表期末现金")
    eb(7, "Short-term investments 短期投资", "短期投资", lambda c, i: f"={pc(c)}7")
    eb(8, "Accounts receivable 应收账款", "应收账款",
       lambda c, i: f"={P}!{c}{RV}*{A}!$B${GR['应收周转天数 DSO']}/365")
    eb(9, "Inventory 存货", "存货",
       lambda c, i: f"={P}!{c}{RC}*{A}!$B${GR['存货周转天数 DIO']}/365")
    eb(10, "Other current assets 其他流动资产", "其他流动资产",
       lambda c, i: f"={P}!{c}{RV}*{A}!$B${GR['其他流动资产 / 营收']}")
    eb(11, "  Total current assets 流动资产合计", None, lambda c, i: f"=SUM({c}6:{c}10)", NUM, True)
    eb(12, "PP&E, net 固定资产净额", "固定资产净额",
       lambda c, i: f"={pc(c)}12-{K}!{c}16-{K}!{c}7", NUM, False,
       "期初 + 资本开支 − 折旧（现金流量表的资本开支为负数，故此处相减）")
    eb(13, "Long-term investments 长期投资", "长期投资", lambda c, i: f"={pc(c)}13")
    eb(14, "Goodwill & intangibles 商誉与无形资产", None, lambda c, i: f"={pc(c)}14-{K}!{c}8",
       NUM, False, "期初 − 无形摊销",
       hist=[None if (hv(BAL, "商誉", i) is None and hv(BAL, "无形资产", i) is None)
             else (hv(BAL, "商誉", i) or 0) + (hv(BAL, "无形资产", i) or 0) for i in range(NH)])
    eb(15, "Other non-current assets 其他非流动资产", "其他非流动资产", lambda c, i: f"={pc(c)}15")
    eb(16, "  Total assets 资产总计", None, lambda c, i: f"={c}11+SUM({c}12:{c}15)", NUM, True)
    wb_.cell(17, 1, "Liabilities & equity 负债与所有者权益").font = F_HDR
    for c in range(1, 2 + NH + NY):
        wb_.cell(17, c).fill = P_N
    eb(18, "Short-term borrowings 短期借款", "短期借款",
       lambda c, i: f"={pc(c)}18+{K}!{c}23", NUM, False, "期初 + 循环贷净变动")
    eb(19, "Current portion of LT debt 一年内到期债务", "一年内到期债务", lambda c, i: f"={pc(c)}19")
    eb(20, "Accounts payable 应付账款", "应付账款",
       lambda c, i: f"={P}!{c}{RC}*{A}!$B${GR['应付周转天数 DPO']}/365")
    eb(21, "Long-term debt 长期债务", "长期债务", lambda c, i: f"={pc(c)}21")
    eb(22, "Other current liabilities 其他流动负债", "其他流动负债",
       lambda c, i: f"={P}!{c}{RV}*{A}!$B${GR['其他流动负债 / 营收']}")
    eb(23, "Other non-current liabilities 其他非流动负债", "其他非流动负债", lambda c, i: f"={pc(c)}23")
    eb(24, "  Total liabilities 负债合计", None,
       lambda c, i: f"=SUM({c}18:{c}23)", NUM, True)
    eb(25, "Stockholders' equity 归母权益", None,
       lambda c, i: (f"={pc(c)}25+{P}!{c}{RNIP}+{K}!{c}20+{K}!{c}21+{K}!{c}22+{P}!{c}{RSBC}"),
       NUM, False, "期初 + 归母净利 + 分红(负) + 回购(负) + 增发 + SBC计入资本公积",
       hist=hrow(BAL, "归母权益"))
    eb(26, "Noncontrolling interests 少数股东权益", "少数股东权益",
       lambda c, i: f"={pc(c)}26+{P}!{c}{RMI}")
    eb(27, "  Total equity 权益合计", None, lambda c, i: f"={c}25+{c}26", NUM, True)
    eb(28, "  Total liabilities & equity 负债和权益总计", None, lambda c, i: f"={c}24+{c}27", NUM, True)
    eb(29, "★ 平衡校验 = 资产 −（负债+权益）", None, lambda c, i: f"={c}16-{c}28", Z, True,
       "必须为 0", P_O)
    eb(30, "Net debt 净负债", None, lambda c, i: f"={c}18+{c}19+{c}21-{c}6-{c}7", NUM, False,
       "有息负债 − 现金 − 短期投资。经营租赁负债【不】计入（ASC 842 下租金已在 EBIT 内扣除）")
    eb(31, "NWC 营运资本", None, lambda c, i: f"={c}8+{c}9+{c}10-{c}20-{c}22")
    # ---- 备查行（不进资产总计，不影响平衡校验）----
    # Damodaran 的研发资本是【报表外调整】：把它塞进资产会破坏与申报报表的对账关系。
    eb(32, "〔备查〕研发摊销 R&D amortization", None,
       lambda c, i: f"={pc(c)}33/{A}!$B${DR['研发摊销年限（年）']}", NUM, False,
       "上期研发资本 ÷ 摊销年限（直线法近似逐年归集口径）")
    eb(33, "〔备查〕研发资本 Research asset", None,
       lambda c, i: f"={pc(c)}33+{P}!{c}{RRD}-{c}32", NUM, False,
       "期初 + 本期研发费用 − 本期摊销（基期值由「1_假设」的期初研发资本给定）",
       hist=[None] * (NH - 1) + [round(float(O.get("rd_asset", 0.0)), 2)])
    eb(34, "〔备查〕投入资本 IC（申报口径）", None,
       lambda c, i: f"={c}31+{c}12+{c}14+{c}15-{c}23", NUM, True,
       "营运资本 + 固定资产 + 商誉无形 + 其他非流动资产 − 其他非流动负债。"
       "等价于融资端「有息债务 + 全部权益 − 现金 − 短投 − 长投」——"
       "两侧恒等（同一恒等式的重排），是口径自洽性展示，不构成独立检错")
    eb(35, "〔备查〕投入资本 IC（含研发资本）", None,
       lambda c, i: f"={c}34+IF({A}!$B${DR['研发资本化开关（1=开 0=关）']}=1,{c}33,0)", NUM, True)
    eb(36, "〔备查〕生效投入资本 IC", None,
       lambda c, i: f"={c}35", NUM, True, "供 DCF 终值使用")

    # ---------- 4_CashFlow（行号固定 6..30）----------
    wk = sheet("4_CashFlow", "2E75B6")
    wk.cell(3, 1, "注：历史列仅填报表已披露科目；营运资本明细变动、循环贷与股数滚动为模型专用行，历史留空。"
            ).font = Font(size=9, color=GREY)
    MC = f"{A}!$B${GR['最低现金余额']}"; SW = f"{A}!$B${GR['现金归集阈值']}"
    FYR = f"{A}!$B${FN['增发到账年份序号（1=首个预测年）']}"

    def ek(row, label, hist, fx, fmt=NUM, bold=False, note=None):
        emit(wk, row, label, hist, fx, fmt, bold, note)

    ek(6, "Net income 净利润", hrow(INC, "净利润"), lambda c, i: f"={P}!{c}{RNI}")
    ek(7, "+ Depreciation 折旧", hrow(CFS, "折旧"),
       lambda c, i: f"={B_}!{pc(c)}12*{A}!$B${GR['折旧率（占期初固定资产净额）']}",
       NUM, False, "期初固定资产净额 × 折旧率（分子分母口径一致）")
    ek(8, "+ Amortization 无形摊销", hrow(CFS, "摊销"),
       lambda c, i: f"={P}!{c}{RV}*{A}!$B${GR['无形摊销 / 营收']}")
    ek(9, "+ SBC 股份支付", hrow(CFS, "股份支付"), lambda c, i: f"={P}!{c}{RSBC}",
       NUM, False, "非现金费用，会计上必然加回 OCF；是否在 FCFF 中加回是另一回事（见 5_DCF）")
    for row, lbl, rr in [(10, "− ΔAccounts receivable 应收增加", 8),
                         (11, "− ΔInventory 存货增加", 9),
                         (12, "− ΔOther current assets 其他流动资产增加", 10)]:
        ek(row, lbl, [None] * NH, lambda c, i, x=rr: f"=-({B_}!{c}{x}-{B_}!{pc(c)}{x})")
    ek(13, "+ ΔAccounts payable 应付增加", [None] * NH,
       lambda c, i: f"={B_}!{c}20-{B_}!{pc(c)}20")
    ek(14, "+ ΔOther current liabilities 其他流动负债增加", [None] * NH,
       lambda c, i: f"={B_}!{c}22-{B_}!{pc(c)}22")
    ek(15, "Operating cash flow 经营活动现金流", hrow(CFS, "经营活动现金流"),
       lambda c, i: f"=SUM({c}6:{c}14)", NUM, True)
    UCX = f"{A}!$B${FN['募资投向 — 资本开支']}"
    ek(16, "− Capital expenditure 资本开支",
       [None if hv(CFS, "资本开支", i) is None else -hv(CFS, "资本开支", i) for i in range(NH)],
       lambda c, i: (f"=-{P}!{c}{RV}*{A}!$E${CX_ROW+i}"
                     f"-IF(AND({UCX}>0,{i+1}>={FYR},{i+1}<={FYR}+2),{UCX}/3,0)"), NUM, False,
       "经营性资本开支 + 募投资本开支（自到账年份起 3 年平摊）")
    ek(17, "Investing cash flow 投资活动现金流", [None] * NH, lambda c, i: f"={c}16", NUM, True)
    ek(20, "− Dividends 分红",
       [None if hv(CFS, "分红", i) is None else -hv(CFS, "分红", i) for i in range(NH)],
       lambda c, i: f"=-{P}!{c}{RNIP}*{A}!$B${GR['派息率（占归母净利）']}")
    ek(21, "− Share repurchase 回购",
       [None if hv(CFS, "回购", i) is None else -hv(CFS, "回购", i) for i in range(NH)],
       lambda c, i: f"=-{P}!{c}{RV}*{A}!$B${FN['年度回购金额 / 营收']}")
    ek(22, "+ Equity issuance 增发募资净额", [None] * NH,
       lambda c, i: f"=IF({FYR}={i+1},{A}!$B${FN['募资净额（公式）']},0)")
    ek(23, "+ Revolver 循环贷净变动", [None] * NH,
       lambda c, i: (f"=IF({c}26<{MC},{MC}-{c}26,"
                     f"IF(AND({c}26>{MC}+{SW},{B_}!{pc(c)}18>0),"
                     f"-MIN({B_}!{pc(c)}18,{c}26-({MC}+{SW})),0))"),
       NUM, False, "现金低于最低余额则借入；高于阈值且有短贷则归还")
    ek(24, "Financing cash flow 筹资活动现金流", [None] * NH,
       lambda c, i: f"=SUM({c}20:{c}23)", NUM, True)
    ek(25, "Net change in cash 现金净增加", [None] * NH,
       lambda c, i: f"={c}15+{c}17+{c}24", NUM, True)
    ek(26, "（辅助）筹资前现金余额", [None] * NH,
       lambda c, i: f"={pc(c)}27+{c}15+{c}17+{c}20+{c}21+{c}22", NUM, False,
       "含回购与增发，循环贷在此之后判断是否触发")
    ek(27, "Ending cash 期末现金", hrow(BAL, "现金及等价物"),
       lambda c, i: f"={pc(c)}27+{c}25", NUM, True)
    ek(28, "  回购股数（百万股）", [None] * NH,
       lambda c, i: f"=-{c}21/{A}!$B${FN['回购均价（美元/股）']}")
    ek(29, "  SBC 增发股数（仅在加回口径下）", [None] * NH,
       lambda c, i: (f"=IF({A}!$B${DR['SBC 加回开关（0=费用化 1=加回）']}=1,"
                     f"{P}!{c}{RSBC}/{A}!$B${FN['回购均价（美元/股）']},0)"),
       NUM, False, "Damodaran：既然现金流已罚过 SBC，就不该再摊薄股数。两者绑在同一开关上")
    # 起点必须是【当前在外股数】，不能用上一年的加权平均摊薄股数 ——
    # 后者是整个财年的平均值，已经含了该年回购的一半效应，再往下滚会把回购算两遍。
    # 实测 AAPL：加权平均 15,005 vs 当前在外 14,594，差 411 百万股（2.8%），
    # 会一路带偏 EPS。这是验证器逐格复算抓出来的。
    ek(30, "Diluted shares 摊薄股数（滚动）", hrow(SHR, "摊薄股数"),
       lambda c, i: ((f"={A}!$B${OR_['摊薄股数（百万股）']}" if i == 0 else f"={pc(c)}30")
                     + f"-{c}28+{c}29+IF({FYR}={i+1},{A}!$B${FN['增发股数（百万股）']},0)"),
       NUM, True,
       "历史列 = 报表的加权平均摊薄股数（仅供参照）；预测起点 = 假设页的当前在外股数")
    ek(18, "Free cash flow (OCF − capex) 自由现金流", [None] * NH,
       lambda c, i: f"={c}15+{c}16", NUM, True)
    ek(19, "FCFF（无杠杆自由现金流）", [None] * NH,
       lambda c, i: (f"={P}!{c}{REBIT}*(1-{A}!$B${GR['有效所得税率']})+{c}7+{c}8"
                     f"+IF({A}!$B${DR['SBC 加回开关（0=费用化 1=加回）']}=1,{c}9,0)"
                     f"+{c}16+SUM({c}10:{c}14)"), NUM, True,
       "EBIT×(1−t) + 折旧 + 摊销 [+ SBC 仅在加回口径下] − 资本开支 + 营运资本变动")
    cal["_ref"] = dict(RV=RV, RC=RC, RGP=RGP, REBIT=REBIT, RPBT=RPBT, RTAX=RTAX, RNI=RNI,
                       RMI=RMI, RNIP=RNIP, RSH=RSH, REPS=REPS, RSBC=RSBC, RIE=RIE, RII=RII)
    return _dcf_sheet(wb, cal, path)


# ============================================================
# 5_DCF
# ============================================================
def _dcf_sheet(wb, cal, path):
    M, PR, DC, GEN, CO = cal["meta"], cal["periods"], cal["dcf"], cal["gen"], cal["course"]
    R, RF = cal["_rows"], cal["_ref"]
    CC, NY, NH = R["CC"], R["NY"], R["NH"]
    GR, OR_, DR, FN = R["GR"], R["OR_"], R["DR"], R["FN"]
    A = "'1_Assumptions'"; P = "'2_Income'"; B_ = "'3_Balance'"; K = "'4_CashFlow'"
    YE = PR["proj"]
    w = wb.create_sheet("5_DCF"); w.sheet_properties.tabColor = RED
    w.column_dimensions["A"].width = 44
    for i in range(2, 3 + NY):
        w.column_dimensions[CL(i)].width = 15
    NOTE = 3 + NY
    w.column_dimensions[CL(NOTE)].width = 78
    w.cell(1, 1, f"{M['name']} ({M['ticker']}) — DCF 估值（全公式，百万美元）").font = F_TTL
    w.cell(2, 1, "绿色单元格链接自「1_Assumptions」，改假设本页自动重算").font = Font(size=9, italic=True)

    def sec(r, t):
        w.cell(r, 1, t).font = F_HDR
        for c in range(1, 3 + NY):
            w.cell(r, c).fill = P_N
        return r + 1

    def put(r, label, val, fmt=NUM, bold=False, note=None, link=False):
        c0 = w.cell(r, 1, label); c0.font = Font(bold=bold, size=10); c0.border = BX
        cc = w.cell(r, 2, val)
        cc.font = F_LNK if link else (Font(bold=True, size=10) if bold else F_FX)
        cc.number_format = fmt
        if note:
            w.cell(r, NOTE, note).font = Font(size=9, color=GREY)
        return r + 1

    def row(r, label, fx, fmt=NUM, bold=False, note=None):
        c0 = w.cell(r, 1, label); c0.font = Font(bold=bold, size=10); c0.border = BX
        for i, src in enumerate(CC):
            cc = w.cell(r, 2 + i, fx(CL(2 + i), src, i)); cc.number_format = fmt
            cc.font = Font(bold=True, size=10) if bold else F_FX
        if note:
            w.cell(r, NOTE, note).font = Font(size=9, color=GREY)
        return r + 1

    r = 4
    r = sec(r, "一、折现率 WACC")
    RFr = r; r = put(r, "无风险利率 Rf（10Y UST）", f"={A}!B{DR['无风险利率 Rf（10Y UST）']}", PCT2, False, None, True)
    ERr = r; r = put(r, "股权风险溢价 ERP", f"={A}!B{DR['股权风险溢价 ERP']}", PCT2, False, None, True)
    BEr = r; r = put(r, "Beta（5年 + Blume）", f"={A}!B{DR['Beta（5年对标普500回归+Blume）']}", "0.000", False, None, True)
    KEr = r; r = put(r, "股权成本 Ke = Rf + β×ERP", f"=B{RFr}+B{BEr}*B{ERr}", PCT2, True)
    RTr = r; r = put(r, "信用评级（合成，可覆盖）", f"={A}!B{DR['信用评级（可覆盖）']}", "@", False,
                     "Altman Z 推导的默认值；已知实际 S&P 评级时请在假设页直接改", True)
    SPr = r; r = put(r, "信用利差", f"={A}!B{DR['信用利差']}", PCT2, False, CO["spread_src"], True)
    KDr = r; r = put(r, "债务成本 Kd = Rf + 信用利差", f"=B{RFr}+B{SPr}", PCT2, True,
                     "课程 Ch.4：kd = 无风险利率 + 国家违约利差 + 公司违约利差（美国视为无国家违约利差）")
    TXr = r; r = put(r, "有效所得税率 t", f"={A}!B{GR['有效所得税率']}", PCT2, False, None, True)
    WDr = r; r = put(r, "债务权重 D/(D+E)", f"={A}!B{DR['债务权重 D/(D+E)']}", PCT2, False, None, True)
    WCr = r; r = put(r, "WACC", f"=(1-B{WDr})*B{KEr}+B{WDr}*B{KDr}*(1-B{TXr})", PCT2, True)
    GTr = r; r = put(r, "永续增长率 g", f"={A}!B{DR['永续增长率 g']}", PCT2, False,
                     "Damodaran 规则：g ≤ Rf。当前 Rf 见上方第 1 行", True)
    MYr = r; r = put(r, "期中折现开关", f"={A}!B{DR['期中折现开关（1=是 0=否）']}", "0", False, None, True)
    SBr = r; r = put(r, "SBC 加回开关（0=费用化 1=加回）",
                     f"={A}!B{DR['SBC 加回开关（0=费用化 1=加回）']}", "0", False,
                     "0 = Damodaran（默认）：SBC 是真实成本，不加回；股数也不因 SBC 摊薄", True)
    r += 1

    r = sec(r, "二、自由现金流 FCFF 与现值")
    w.cell(r, 1, "科目（百万美元）").font = F_COL; w.cell(r, 1).fill = P_B
    for i, y in enumerate(YE):
        cc = w.cell(r, 2 + i, y); cc.font = F_COL; cc.fill = P_B
        cc.alignment = Alignment(horizontal="center")
    r += 1
    Er = r;  r = row(r, "EBIT（= Operating income）", lambda c, s, i: f"={P}!{s}{RF['REBIT']}")
    EAr = r; r = row(r, "EBIT × (1 − t)", lambda c, s, i: f"={c}{Er}*(1-$B${TXr})")
    DPr = r; r = row(r, "+ 折旧", lambda c, s, i: f"={K}!{s}7")
    AMr = r; r = row(r, "+ 无形摊销", lambda c, s, i: f"={K}!{s}8")
    SCr = r; r = row(r, "+ SBC（仅在加回口径下）",
                     lambda c, s, i: f"=IF($B${SBr}=1,{K}!{s}9,0)", NUM, False,
                     "默认 0。加回会同时打开股数的 SBC 摊薄 —— 两件事绑在一起")
    CXr = r; r = row(r, "− 资本开支", lambda c, s, i: f"={K}!{s}16")
    WKr = r; r = row(r, "营运资本变动（现金影响）", lambda c, s, i: f"=SUM({K}!{s}10:{s}14)")
    FFr = r; r = row(r, "FCFF", lambda c, s, i: f"={c}{EAr}+{c}{DPr}+{c}{AMr}+{c}{SCr}+{c}{CXr}+{c}{WKr}", NUM, True)
    Tr = r;  r = row(r, "折现年数 t", lambda c, s, i: i + 1, "0")
    DFr = r; r = row(r, "折现因子", lambda c, s, i: f"=1/(1+$B${WCr})^({c}{Tr}-0.5*$B${MYr})", "0.0000")
    PVr = r; r = row(r, "FCFF 现值", lambda c, s, i: f"={c}{FFr}*{c}{DFr}", NUM, True)
    r += 1

    r = sec(r, "三、终值（Damodaran 一致性：稳态再投资率 = g / ROIC）")
    TTr = r; r = put(r, "终值所得税率 t_term", f"={A}!B{DR['终值所得税率 t_term']}", PCT2, False,
                     "美国联邦法定 21%；永续期不应假设税收优惠永存", True)
    TEr = r; r = put(r, f"{YE[-1]} EBIT", f"={CL(1+NY)}{Er}")
    TNr = r; r = put(r, f"{YE[-1]} 营运资本 NWC", f"={B_}!{CC[-1]}31")
    TPr = r; r = put(r, f"{YE[-1]} 固定资产净额", f"={B_}!{CC[-1]}12")
    TGr = r; r = put(r, f"{YE[-1]} 商誉与无形资产", f"={B_}!{CC[-1]}14")
    TOr = r; r = put(r, f"{YE[-1]} 其他非流动资产 − 其他非流动负债",
                     f"={B_}!{CC[-1]}15-{B_}!{CC[-1]}23")
    ICBr = r; r = put(r, "投入资本 IC（申报口径）", f"=B{TNr}+B{TPr}+B{TGr}+B{TOr}", NUM, True,
                      "口径：营运资本 + 固定资产 + 商誉无形 + 其他非流动资产 − 其他非流动负债。"
                      "旧版只算「营运资本 + 固定资产」，对美股大盘会崩：AAPL 营运资本 −520 亿、"
                      "商誉无形与其他非流动资产未计入 → IC = −22 亿 → ROIC −4760%，终值被迫降级。"
                      "本式等价于融资端「有息债务 + 全部权益 − 现金 − 短投 − 长投」；"
                      "两侧恒等（同一恒等式的重排），属口径自洽性展示，不是独立检错")
    TRr = r; r = put(r, f"{YE[-1]} 研发资本（Damodaran）", f"={B_}!{CC[-1]}33", NUM, False,
                     "报表外调整项：不进资产总计，只用于投入资本与 ROIC")
    ICr = r; r = put(r, "生效投入资本 IC",
                     f"=B{ICBr}+IF({A}!$B${DR['研发资本化开关（1=开 0=关）']}=1,B{TRr},0)", NUM, True)
    RBr = r; r = put(r, "终值 ROIC（计算值）", f"=B{TEr}*(1-B{TTr})/B{ICr}", PCT2, False,
                     "把末年超额回报直接永续化")
    RCr = r; r = put(r, "终值 ROIC（收敛口径）",
                     f"=B{WCr}+{A}!$B${DR['终值 ROIC 收敛溢价（模式2 用）']}", PCT2, False,
                     "竞争终将侵蚀超额回报：终值 ROIC = WACC + 溢价")
    ROr = r; r = put(r, "终值 ROIC（生效）",
                     f"=IF({A}!$B${DR['终值 ROIC 处理（1=沿用末年 2=收敛至 WACC+溢价）']}=2,"
                     f"B{RCr},B{RBr})", PCT2, True, "必须可持续，否则终值不成立")
    RRr = r; r = put(r, "稳态再投资率 RR = g / ROIC", f"=B{GTr}/B{ROr}", PCT2, True,
                     "Damodaran：g = RR × ROIC，三者不可自由设定")
    NPr = r; r = put(r, "终值 NOPAT = EBIT × (1 − t_term)", f"=B{TEr}*(1-B{TTr})", NUM, False,
                     "用【申报口径】NOPAT：研发资本化只调 ROIC 与 IC，不调 FCFF。"
                     "稳态下研发支出≈研发摊销，两种口径的 NOPAT 收敛，故此处一致")
    DEr = r; r = put(r, "★ 终值是否降级（1=是）",
                     f"=IF(OR(B{ICr}<=0,B{ROr}<=B{WCr},B{RRr}>=1,B{RRr}<0),1,0)", "0", True,
                     "投入资本为负、或 ROIC ≤ WACC、或再投资率越界时，Gordon 式的 (1−g/ROIC) "
                     "会失真甚至变号。此时降级为【无增长永续】TV = NOPAT/WACC —— "
                     "经济上正确：ROIC 不高于 WACC 时增长不创造价值。"
                     "修正 IC 口径后，负投入资本这一触发条件对正常经营的公司已不应出现")
    TFr = r; r = put(r, "终值 FCFF",
                     f"=IF(B{DEr}=1,B{NPr},B{TEr}*(1+B{GTr})*(1-B{TTr})*(1-B{RRr}))", NUM, True)
    TVr = r; r = put(r, "终值 TV",
                     f"=IF(B{DEr}=1,B{NPr}/B{WCr},B{TFr}/(B{WCr}-B{GTr}))", NUM, True,
                     "未降级：FCFF终 /(WACC − g)　｜　已降级：NOPAT / WACC")
    PTr = r; r = put(r, "终值现值 PV(TV)", f"=B{TVr}/(1+B{WCr})^({NY}-0.5*B{MYr})", NUM, True)
    GSr = r; r = put(r, "〔交叉校验〕可持续增长 g =（1−派息率）× ROE",
                     CO["g_sustainable"], PCT, False,
                     ("课程公式表。与上方 g = RR × ROIC 是两个口径（股权侧 vs 企业侧），"
                      "差距过大说明终值的增长/再投资/回报三者不自洽"
                      if CO["g_sust_usable"] else
                      f"⚠ 本例不可用：{CO['g_sust_note']}"))
    r += 1

    r = sec(r, "四、企业价值 → 每股内在价值")
    S1 = r; r = put(r, "明确期 FCFF 现值合计", f"=SUM(B{PVr}:{CL(1+NY)}{PVr})")
    S2 = r; r = put(r, "终值现值", f"=B{PTr}")
    EV0 = r; r = put(r, f"企业价值 EV（{PR['annual'][-1]} 基准）", f"=B{S1}+B{S2}", NUM, True)
    RLr = r; r = put(r, "估值基准日滚动年数", f"={A}!B{DR['估值基准日滚动年数']}", "0.000", False,
                     "财年末 → 评估日。非 12-31 财年在此体现", True)
    EVr = r; r = put(r, "企业价值 EV（滚动至评估日）",
                     f"=B{EV0}*(1+B{WCr})^B{RLr}-B{FFr}*B{RLr}*(1+B{WCr})^(B{RLr}/2)", NUM, True,
                     "滚动后扣除财年末至评估日已实现的自由现金流")
    NDr = r; r = put(r, "减：净负债（基期）",
                     f"={A}!B{OR_['短期借款']}+{A}!B{OR_['一年内到期债务']}+{A}!B{OR_['长期债务']}"
                     f"-{A}!B{OR_['现金及等价物']}-{A}!B{OR_['短期投资']}", NUM, False,
                     "经营租赁负债【不】计入 —— ASC 842 下租金已在 EBIT 内扣除，再计入即重复扣")
    NOr = r; r = put(r, "加：非经营资产", f"={A}!B{DR['非经营资产（加回）']}", NUM, False,
                     "长期有价证券与权益法投资（短期投资已在净负债中扣除）", True)
    FNr = r; r = put(r, "加：增发募资净额（现值）",
                     f"={A}!B{FN['募资净额（公式）']}/(1+B{WCr})^{A}!B{FN['桥接折现年数']}", NUM, False,
                     "净负债按基期计量，未来募资的现金对 EV 不可见，必须单列", True)
    GXr = r; r = put(r, "归属全体股东权益价值", f"=B{EVr}-B{NDr}+B{NOr}+B{FNr}", NUM, True)
    MMr = r; r = put(r, "少数股东扣除方式", f"={A}!B{DR['少数股东扣除方式（1=账面 2=价值）']}", "0", False, None, True)
    MSr = r; r = put(r, "少数股东利润占比", f"={A}!B{GR['少数股东损益占比']}", PCT2, False, None, True)
    MIr = r; r = put(r, "减：少数股东权益",
                     f"=IF(B{MMr}=1,{A}!B{OR_['少数股东权益']},B{GXr}*B{MSr})")
    EQr = r; r = put(r, "归母股权价值", f"=B{GXr}-B{MIr}", NUM, True)
    SHr = r; r = put(r, "当前摊薄股数（百万股，含增发）",
                     f"={A}!B{OR_['摊薄股数（百万股）']}+{A}!B{FN['增发股数（百万股）']}", NUM, False,
                     "用【当前】股数而非终值年股数：FCFF 是融资前口径，回购属于融资决策，"
                     "不改变今天的企业价值；回购对未来 EPS 的影响见「2_Income」")
    PSr = r; r = put(r, "每股内在价值", f"=B{EQr}/B{SHr}", USD, True)
    PRr = r; r = put(r, "现价", f"={A}!B{OR_['现价（美元）']}", USD)
    UPr = r; r = put(r, "上行 / 下行空间", f"=B{PSr}/B{PRr}-1", PCT, True)
    TWr = r; r = put(r, "终值占 EV 比重", f"=B{S2}/B{EV0}", PCT, False, "超过 75% 说明估值高度依赖终值假设")
    r += 1

    r = sec(r, "五、退出倍数交叉验证（FINC6021 Ch.8：倍数与 DCF 是同一件事）")
    EBr = r; r = put(r, f"{YE[-1]} EBITDA", f"=B{TEr}+{CL(1+NY)}{DPr}+{CL(1+NY)}{AMr}", NUM, True)
    XMr = r; r = put(r, "模型隐含的退出 EV/EBITDA", f"=B{TVr}/B{EBr}", MX, True,
                     "由 Gordon 终值反解。若远高于同业成熟公司的 EV/EBITDA，说明终值假设过于乐观")
    XPr = r; r = put(r, f"{YE[-1]} 归母净利", f"={P}!{CC[-1]}{RF['RNIP']}")
    XEr = r; r = put(r, "模型隐含的退出 P/E（终值权益 / 终值净利）",
                     f"=(B{TVr}-({A}!B{OR_['短期借款']}+{A}!B{OR_['一年内到期债务']}+{A}!B{OR_['长期债务']}))/B{XPr}",
                     MX, False, "粗略口径：终值 EV 扣基期有息负债。仅作量级对照")
    TMr = r; r = put(r, "理论倍数 = (1+g)/(WACC − g)", f"=(1+B{GTr})/(B{WCr}-B{GTr})", MX, False,
                     "课程：资本化率 = 1/倍数 < 折现率；增长越高，两者差距越大")
    r += 1

    r = sec(r, "六、资本结构漂移诊断（FINC6021 Ch.4：WACC 假定资本结构恒定）")
    w.cell(r, 1, "年度").font = F_COL; w.cell(r, 1).fill = P_B
    for i, y in enumerate(YE):
        cc = w.cell(r, 2 + i, y); cc.font = F_COL; cc.fill = P_B
        cc.alignment = Alignment(horizontal="center")
    r += 1
    DBr = r; r = row(r, "模型内含有息负债", lambda c, s, i: f"={B_}!{s}18+{B_}!{s}19+{B_}!{s}21")
    EMr = r; r = row(r, "股权（按现价 × 滚动股数）",
                     lambda c, s, i: f"={A}!$B${OR_['现价（美元）']}*{K}!{s}30")
    WWr = r; r = row(r, "模型内含 D/(D+E)", lambda c, s, i: f"={c}{DBr}/({c}{DBr}+{c}{EMr})", PCT2, True)
    r = row(r, "与 WACC 假设权重的差", lambda c, s, i: f"={c}{WWr}-$B${WDr}", PCT2, False,
            "Plug 选的是现金 + 循环贷，资本结构会漂移；WACC 却假定它恒定。"
            "差距大到一定程度就该改用目标资本结构，并对 β 做去杠杆/再杠杆")
    r += 1

    r = sec(r, "七、敏感性：每股内在价值（行 = WACC，列 = 永续增长率 g）")
    SHROW = r
    w.cell(r, 1, "WACC \\ g").font = F_COL; w.cell(r, 1).fill = P_B
    GS = [.015, .020, .025, .030, .035]
    for i, gg in enumerate(GS):
        cc = w.cell(r, 2 + i, gg); cc.font = F_COL; cc.fill = P_B; cc.number_format = PCT2
    r += 1
    for off in (-0.02, -0.01, 0.0, 0.01, 0.02):
        cc = w.cell(r, 1, f"=$B${WCr}+{off}"); cc.font = F_COL; cc.fill = P_H; cc.number_format = PCT2
        for i in range(5):
            col = CL(2 + i)
            pv = "+".join(f"${CL(2+t)}${FFr}/(1+$A{r})^({t+1}-0.5*$B${MYr})" for t in range(NY))
            roic = f"($B${TEr}*(1-$B${TTr})/$B${ICr})"
            rr_ = f"({col}${SHROW}/{roic})"
            nopat = f"($B${TEr}*(1-$B${TTr}))"
            deg = f"OR($B${ICr}<=0,{roic}<=$A{r},{rr_}>=1,{rr_}<0)"
            tv = (f"IF({deg},{nopat}/$A{r},"
                  f"$B${TEr}*(1+{col}${SHROW})*(1-$B${TTr})*(1-{rr_})/($A{r}-{col}${SHROW}))")
            tvpv = f"({tv})/(1+$A{r})^({NY}-0.5*$B${MYr})"
            ev = (f"(({pv})+{tvpv})*(1+$A{r})^$B${RLr}"
                  f"-$B${FFr}*$B${RLr}*(1+$A{r})^($B${RLr}/2)")
            gx = f"({ev}-$B${NDr}+$B${NOr}+$B${FNr})"
            eq = f"{gx}*(1-IF($B${MMr}=1,0,$B${MSr}))-IF($B${MMr}=1,{A}!$B${OR_['少数股东权益']},0)"
            cc = w.cell(r, 2 + i, f"=({eq})/$B${SHr}"); cc.font = F_FX; cc.number_format = USD
        r += 1
    w.cell(r, 1, "★ 中心格（生效 WACC，且 g = 生效值）应等于上方「每股内在价值」").font = Font(size=9, color="006400")

    _engine(wb, cal, dict(WC=WCr, GT=GTr, MY=MYr, RL=RLr, TT=TTr, ND=NDr, NO=NOr, FN=FNr,
                          MM=MMr, MS=MSr, SH=SHr, PS=PSr, GSTART=r + 2, DCF=w))
    wb.save(path)
    return dict(path=path, rows=dict(PS=PSr, WC=WCr, GT=GTr, EQ=EQr, GX=GXr, EV=EVr, EV0=EV0,
                                     ND=NDr, NO=NOr, FN=FNr, SH=SHr, FF=FFr, TT=TTr, RO=ROr,
                                     RR=RRr, TF=TFr, TV=TVr, IC=ICr, S1=S1, S2=S2, TW=TWr,
                                     MI=MIr, MM=MMr, MS=MSr, RL=RLr, MY=MYr, SB=SBr))


# ============================================================
# 6_Engine 敏感性引擎：每行 = 一次完整的 N 年 FCFF 重算
# ============================================================
# 可行的前提：FCFF = EBIT(1−t) + 折旧 + 摊销 − 资本开支 − ΔNWC，
# 而 US GAAP 的 OperatingIncomeLoss 本就在利息之前 —— FCFF 与现金余额、循环贷 plug 完全无关，
# 因此可以在【单行内】闭合算完一个 N 年模型，网格里每个格子就是一行。
def _engine(wb, cal, D_):
    M = cal["meta"]
    R = cal["_rows"]
    GR, OR_, DR = R["GR"], R["OR_"], R["DR"]
    NY = R["NY"]; G_ROW, M_ROW, CX_ROW, SB_ROW = R["G_ROW"], R["M_ROW"], R["CX_ROW"], R["SB_ROW"]
    A = "'1_Assumptions'"; DS = "'5_DCF'"
    w = wb.create_sheet("6_Engine"); w.sheet_properties.tabColor = "7F7F7F"
    w.column_dimensions["A"].width = 26
    w.cell(1, 1, f"{M['ticker']} — 敏感性引擎（每行 = 一次完整 {NY} 年 FCFF 重算）").font = \
        Font(bold=True, size=13, color=NAVY)
    w.cell(2, 1, "本页是计算中间层，不需手工编辑。「5_DCF」的敏感性表从这里取数；"
                 "改「1_Assumptions」后本页自动重算。").font = Font(size=9, italic=True, color=GREY)

    r = 4
    w.cell(r, 1, "基期锚点与生效参数").font = F_HDR
    for c in range(1, 4):
        w.cell(r, c).fill = P_N
    r += 1
    G = lambda k: f"{A}!$B${GR[k]}"
    OO = lambda k: f"{A}!$B${OR_[k]}"
    anchors = [
        ("基期营业收入", f"={OO('基期营业收入')}"),
        ("期初固定资产净额", f"={OO('固定资产净额')}"),
        ("期初商誉与无形", f"={OO('商誉与无形资产')}"),
        ("期初研发资本", f"={OO('期初研发资本（Damodaran）')}"),
        ("其他非流动资产净额", f"={OO('其他非流动资产')}-{OO('其他非流动负债')}"),
        ("期初 NWC", f"={OO('应收账款')}+{OO('存货')}+{OO('其他流动资产')}"
                     f"-{OO('应付账款')}-{OO('其他流动负债')}"),
        ("经营费用率合计", f"={G('研发费用 / 营收')}+{G('销售及管理费用 / 营收')}"),
        ("其他经营损益率", f"={G('其他经营损益 / 营收')}"),
        ("折旧率", f"={G('折旧率（占期初固定资产净额）')}"),
        ("无形摊销率", f"={G('无形摊销 / 营收')}"),
        ("有效税率", f"={G('有效所得税率')}"),
        ("研发费用率", f"={G('研发费用 / 营收')}"),
        ("研发资本化开关", f"={A}!B{DR['研发资本化开关（1=开 0=关）']}"),
        ("研发摊销年限", f"={A}!B{DR['研发摊销年限（年）']}"),
        ("SBC 加回开关", f"={A}!B{DR['SBC 加回开关（0=费用化 1=加回）']}"),
        ("终值税率", f"={DS}!B{D_['TT']}"),
        ("WACC（生效）", f"={DS}!B{D_['WC']}"),
        ("永续 g（生效）", f"={DS}!B{D_['GT']}"),
        ("期中折现开关", f"={DS}!B{D_['MY']}"),
        ("滚动年数", f"={DS}!B{D_['RL']}"),
        ("净负债", f"={DS}!B{D_['ND']}"),
        ("非经营资产", f"={DS}!B{D_['NO']}"),
        ("募资净额现值", f"={DS}!B{D_['FN']}"),
        ("少数股东方式", f"={DS}!B{D_['MM']}"),
        ("少数股东占比", f"={DS}!B{D_['MS']}"),
        ("账面少数股东权益", f"={OO('少数股东权益')}"),
        ("总股本", f"={DS}!B{D_['SH']}"),
        ("终值ROIC模式", f"={A}!B{DR['终值 ROIC 处理（1=沿用末年 2=收敛至 WACC+溢价）']}"),
        ("终值ROIC溢价", f"={A}!B{DR['终值 ROIC 收敛溢价（模式2 用）']}"),
    ]
    AR = {}
    for lbl, f in anchors:
        w.cell(r, 1, lbl).font = Font(size=10); w.cell(r, 1).border = BX
        cc = w.cell(r, 2, f); cc.font = F_LNK
        cc.number_format = ("0.0000" if any(k in lbl for k in
                            ("率", "开关", "年限", "年数", "方式", "占比", "g（")) else NUM)
        AR[lbl] = r; r += 1
    r += 1

    C0, W_ = 4, 11
    yc = lambda t, k: CL(C0 + W_ * (t - 1) + k)
    SUM0 = C0 + W_ * NY
    SC = {n_: CL(SUM0 + i) for i, n_ in enumerate(
        ["PV", "IC", "ROIC", "RR", "DEG", "NOPAT", "TF", "TV", "PVTV",
         "EV0", "EV", "GX", "MI", "EQ", "PS"])}
    for i in range(C0, SUM0 + 16):
        w.column_dimensions[CL(i)].width = 11
    GRIDS = []

    def block(title, xs, ys, xlabel, ylabel, mode, note):
        nonlocal r
        w.cell(r, 1, title).font = F_HDR
        for c in range(1, SUM0 + 16):
            w.cell(r, c).fill = P_N
        r += 1
        w.cell(r, 1, note).font = Font(size=9, color=RED); r += 1
        hdr = ["场景", xlabel, ylabel]
        for t in range(1, NY + 1):
            hdr += [f"营收{t}", f"成本{t}", f"EBIT{t}", f"折旧{t}", f"资本开支{t}", f"期末固资{t}",
                    f"摊销{t}", f"商誉无形{t}", f"研发资本{t}", f"NWC{t}", f"FCFF{t}"]
        hdr += ["PV明确期", "终值IC", "终值ROIC", "再投资率", "降级", "终值NOPAT",
                "终值FCFF", "TV", "PV(TV)", "EV0", "EV", "全体权益", "少数股东", "归母权益", "每股"]
        for i, h in enumerate(hdr):
            cc = w.cell(r, 1 + i, h); cc.font = F_COL; cc.fill = P_B
            cc.alignment = Alignment(horizontal="center")
        r += 1
        start = r
        for xi, xv in enumerate(xs):
            for yi, yv in enumerate(ys):
                w.cell(r, 1, f"{xi+1}-{yi+1}").font = Font(size=9, color=GREY)
                if mode == "wacc_gm":
                    cx = w.cell(r, 2, f"=$B${AR['WACC（生效）']}+{xv}")
                else:
                    cx = w.cell(r, 2, xv)
                cx.font = F_IN; cx.number_format = PCT2
                cy = w.cell(r, 3, yv); cy.font = F_IN; cy.number_format = PCT2
                LG = lambda t: f"{A}!$E${G_ROW+t-1}"
                LM = lambda t: f"{A}!$E${M_ROW+t-1}"
                _SW = (R.get("IT") or {}).get("锚定开关（1=锚定 0=按增速外推）")

                def _d(t, dcell):
                    # 锚定开启时第 1 年是既成事实，敏感性的 Δ 不该移动它
                    return f"IF({A}!$B${_SW}=1,0,{dcell})" if (t == 1 and _SW) else dcell
                LW = f"$B${AR['WACC（生效）']}"; LGT = f"$B${AR['永续 g（生效）']}"
                if mode == "g_gterm":
                    gexp = lambda t: f"({LG(t)}+{_d(t, f'$B{r}')})"; gmexp = LM
                    wexp = LW; gtexp = f"$C{r}"
                elif mode == "gm_gterm":
                    gexp = LG; gmexp = lambda t: f"({LM(t)}+{_d(t, f'$B{r}')})"
                    wexp = LW; gtexp = f"$C{r}"
                elif mode == "g_gm":
                    gexp = lambda t: f"({LG(t)}+{_d(t, f'$B{r}')})"
                    gmexp = lambda t: f"({LM(t)}+{_d(t, f'$C{r}')})"; wexp = LW; gtexp = LGT
                else:
                    gexp = LG; gmexp = lambda t: f"({LM(t)}+{_d(t, f'$C{r}')})"
                    wexp = f"$B{r}"; gtexp = LGT
                for t in range(1, NY + 1):
                    p_rev = f"$B${AR['基期营业收入']}" if t == 1 else f"{yc(t-1,0)}{r}"
                    p_ppe = f"$B${AR['期初固定资产净额']}" if t == 1 else f"{yc(t-1,5)}{r}"
                    p_gwi = f"$B${AR['期初商誉与无形']}" if t == 1 else f"{yc(t-1,7)}{r}"
                    p_rda = f"$B${AR['期初研发资本']}" if t == 1 else f"{yc(t-1,8)}{r}"
                    p_nwc = f"$B${AR['期初 NWC']}" if t == 1 else f"{yc(t-1,9)}{r}"
                    w[f"{yc(t,0)}{r}"] = f"={p_rev}*(1+{gexp(t)})"
                    w[f"{yc(t,1)}{r}"] = f"={yc(t,0)}{r}*(1-{gmexp(t)})"
                    w[f"{yc(t,2)}{r}"] = (f"={yc(t,0)}{r}-{yc(t,1)}{r}"
                                          f"-{yc(t,0)}{r}*$B${AR['经营费用率合计']}"
                                          f"+{yc(t,0)}{r}*$B${AR['其他经营损益率']}")
                    w[f"{yc(t,3)}{r}"] = f"={p_ppe}*$B${AR['折旧率']}"
                    w[f"{yc(t,4)}{r}"] = f"={yc(t,0)}{r}*{A}!$E${CX_ROW+t-1}"
                    w[f"{yc(t,5)}{r}"] = f"={p_ppe}+{yc(t,4)}{r}-{yc(t,3)}{r}"
                    w[f"{yc(t,6)}{r}"] = f"={yc(t,0)}{r}*$B${AR['无形摊销率']}"
                    w[f"{yc(t,7)}{r}"] = f"={p_gwi}-{yc(t,6)}{r}"
                    w[f"{yc(t,8)}{r}"] = (f"={p_rda}+{yc(t,0)}{r}*$B${AR['研发费用率']}"
                                          f"-{p_rda}/$B${AR['研发摊销年限']}")
                    w[f"{yc(t,9)}{r}"] = (f"={yc(t,0)}{r}*{G('应收周转天数 DSO')}/365"
                                          f"+{yc(t,1)}{r}*{G('存货周转天数 DIO')}/365"
                                          f"+{yc(t,0)}{r}*{G('其他流动资产 / 营收')}"
                                          f"-{yc(t,1)}{r}*{G('应付周转天数 DPO')}/365"
                                          f"-{yc(t,0)}{r}*{G('其他流动负债 / 营收')}")
                    w[f"{yc(t,10)}{r}"] = (f"={yc(t,2)}{r}*(1-$B${AR['有效税率']})"
                                           f"+{yc(t,3)}{r}+{yc(t,6)}{r}"
                                           f"+IF($B${AR['SBC 加回开关']}=1,"
                                           f"{yc(t,0)}{r}*{A}!$E${SB_ROW+t-1},0)"
                                           f"-{yc(t,4)}{r}-({yc(t,9)}{r}-{p_nwc})")
                    for k in range(W_):
                        w[f"{yc(t,k)}{r}"].number_format = NUM
                        w[f"{yc(t,k)}{r}"].font = F_FX
                pv = "+".join(f"{yc(t,10)}{r}/(1+{wexp})^({t}-0.5*$B${AR['期中折现开关']})"
                              for t in range(1, NY + 1))
                w[f"{SC['PV']}{r}"] = f"={pv}"
                w[f"{SC['IC']}{r}"] = (f"={yc(NY,9)}{r}+{yc(NY,5)}{r}+{yc(NY,7)}{r}"
                                       f"+$B${AR['其他非流动资产净额']}"
                                       f"+IF($B${AR['研发资本化开关']}=1,{yc(NY,8)}{r},0)")
                w[f"{SC['NOPAT']}{r}"] = f"={yc(NY,2)}{r}*(1-$B${AR['终值税率']})"
                w[f"{SC['ROIC']}{r}"] = (f"=IF($B${AR['终值ROIC模式']}=2,"
                                         f"{wexp}+$B${AR['终值ROIC溢价']},"
                                         f"IF({SC['IC']}{r}=0,0,{SC['NOPAT']}{r}/{SC['IC']}{r}))")
                w[f"{SC['RR']}{r}"] = f"=IF({SC['ROIC']}{r}=0,9,{gtexp}/{SC['ROIC']}{r})"
                w[f"{SC['DEG']}{r}"] = (f"=IF(OR({SC['IC']}{r}<=0,{SC['ROIC']}{r}<={wexp},"
                                        f"{SC['RR']}{r}>=1,{SC['RR']}{r}<0),1,0)")
                w[f"{SC['TF']}{r}"] = (f"=IF({SC['DEG']}{r}=1,{SC['NOPAT']}{r},"
                                       f"{yc(NY,2)}{r}*(1+{gtexp})*(1-$B${AR['终值税率']})"
                                       f"*(1-{SC['RR']}{r}))")
                w[f"{SC['TV']}{r}"] = (f"=IF({SC['DEG']}{r}=1,{SC['NOPAT']}{r}/{wexp},"
                                       f"{SC['TF']}{r}/({wexp}-{gtexp}))")
                w[f"{SC['PVTV']}{r}"] = (f"={SC['TV']}{r}/(1+{wexp})"
                                         f"^({NY}-0.5*$B${AR['期中折现开关']})")
                w[f"{SC['EV0']}{r}"] = f"={SC['PV']}{r}+{SC['PVTV']}{r}"
                w[f"{SC['EV']}{r}"] = (f"={SC['EV0']}{r}*(1+{wexp})^$B${AR['滚动年数']}"
                                       f"-{yc(1,10)}{r}*$B${AR['滚动年数']}"
                                       f"*(1+{wexp})^($B${AR['滚动年数']}/2)")
                w[f"{SC['GX']}{r}"] = (f"={SC['EV']}{r}-$B${AR['净负债']}+$B${AR['非经营资产']}"
                                       f"+$B${AR['募资净额现值']}")
                w[f"{SC['MI']}{r}"] = (f"=IF($B${AR['少数股东方式']}=1,$B${AR['账面少数股东权益']},"
                                       f"{SC['GX']}{r}*$B${AR['少数股东占比']})")
                w[f"{SC['EQ']}{r}"] = f"={SC['GX']}{r}-{SC['MI']}{r}"
                w[f"{SC['PS']}{r}"] = f"={SC['EQ']}{r}/$B${AR['总股本']}"
                for n_, c_ in SC.items():
                    w[f"{c_}{r}"].font = F_FX
                    w[f"{c_}{r}"].number_format = (PCT2 if n_ in ("ROIC", "RR")
                                                   else USD if n_ == "PS"
                                                   else "0" if n_ == "DEG" else NUM)
                r += 1
        r += 1
        GRIDS.append(dict(title=title, xs=xs, ys=ys, xlabel=xlabel, ylabel=ylabel,
                          start=start, note=note))

    gt = cal["dcf"]["g"]
    GS = [round(gt - 0.015 + 0.0075 * i, 4) for i in range(5)]
    DG = [-0.06, -0.03, 0.0, 0.03, 0.06]
    DM = [-0.04, -0.02, 0.0, 0.02, 0.04]
    DW = [-0.02, -0.01, 0.0, 0.01, 0.02]
    block("模块 A：营收增速平移 × 永续增长率 g", DG, GS, "营收增速 Δ", "永续 g",
          "g_gterm", "Δ 施加在每一年的生效增速上；中心格 Δ=0 且 g=生效值，必然复现基准每股价值")
    block("模块 B：毛利率平移 × 永续增长率 g", DM, GS, "毛利率 Δ", "永续 g",
          "gm_gterm", "毛利率是判断项里最敏感的一个：1pp 的持续偏差会被终值放大")
    block("模块 C：营收增速平移 × 毛利率平移", DG, DM, "营收增速 Δ", "毛利率 Δ",
          "g_gm", "两个判断项同时动 —— 历史上它们并非独立")
    block("模块 D：WACC × 毛利率平移", DW, DM, "WACC 偏移", "毛利率 Δ",
          "wacc_gm", "WACC 偏移直接加在生效 WACC 上，不改 Beta/ERP")
    cal["_grids"] = dict(sheet="6_Engine", grids=GRIDS, PS=SC["PS"])
    _grid_tables(wb, cal, D_)
    _ratios(wb, cal, D_)


def _grid_tables(wb, cal, D_):
    """把引擎算好的格子渲染成 2 维表，贴在「5_DCF」下方。

    中心格自检：每个模块的中心格（Δ 都为 0、g 与 WACC 都取生效值）
    必然等于上方的「每股内在价值」—— 不等就说明引擎与主模型走偏了。
    这是引擎唯一也是最关键的一致性断言。"""
    g = cal["_grids"]
    w = D_["DCF"]; E = f"'{g['sheet']}'"
    r = D_["GSTART"]
    for gd in g["grids"]:
        w.cell(r, 1, gd["title"] + "　—　每股内在价值").font = F_HDR
        for c in range(1, 3 + len(gd["ys"])):
            w.cell(r, c).fill = P_N
        r += 1
        w.cell(r, 1, gd["note"]).font = Font(size=9, color=GREY); r += 1
        cc = w.cell(r, 1, gd["xlabel"] + " \\ " + gd["ylabel"])
        cc.font = F_COL; cc.fill = P_B
        for j, yv in enumerate(gd["ys"]):
            cc = w.cell(r, 2 + j, yv); cc.font = F_COL; cc.fill = P_B; cc.number_format = PCT2
        r += 1
        for i, xv in enumerate(gd["xs"]):
            src = gd["start"] + i * len(gd["ys"])
            cc = w.cell(r, 1, f"={E}!$B${src}"); cc.font = F_COL; cc.fill = P_H
            cc.number_format = PCT2
            for j in range(len(gd["ys"])):
                cc = w.cell(r, 2 + j, f"={E}!{g['PS']}{src + j}")
                cc.font = F_FX; cc.number_format = USD
            r += 1
        mid = gd["start"] + (len(gd["xs"]) // 2) * len(gd["ys"]) + len(gd["ys"]) // 2
        w.cell(r, 1, "★ 中心格自检（应为 0）").font = Font(size=9, color="006400")
        cc = w.cell(r, 2, f"={E}!{g['PS']}{mid}-B{D_['PS']}")
        cc.font = F_FX; cc.number_format = "0.00000000"; cc.fill = P_O
        r += 2


# ============================================================
# 图表样式：对齐卖方研究报告版式
#   深红/卡其/藏蓝三色主调 · 标题左对齐红色加粗 · 图例置顶
#   仅浅色横向网格 · 无边框无填充 · 折线带圆点 · 数据标签直出
# ============================================================
C_RED="9E2B25"; C_KHAKI="C4A85A"; C_NAVY="1F3864"; C_CORAL="C0504D"
C_SLATE="8497B0"; C_GREY="A6A6A6"; C_GRID="D9D9D9"; C_TXT="595959"
PALETTE=[C_RED, C_KHAKI, C_NAVY, C_CORAL, C_SLATE, C_GREY]

def _rt(text, size=1100, bold=True, color=C_RED, align="l"):
    cp=CharacterProperties(sz=size, b=bold, solidFill=color)
    p=Paragraph(pPr=ParagraphProperties(defRPr=cp, algn=align), endParaRPr=cp,
                r=[RegularTextRun(t=text, rPr=cp)])
    t=Title(tx=Text(rich=RichText(p=[p])))
    t.overlay=False
    # 手工锚定到左上角：避免标题框居中后与其他元素争位
    t.layout=Layout(manualLayout=ManualLayout(xMode="edge", yMode="edge", x=0.01, y=0.01))
    return t

def _axis_text(size=900, color=C_TXT):
    cp=CharacterProperties(sz=size, solidFill=color)
    return RichText(p=[Paragraph(pPr=ParagraphProperties(defRPr=cp), endParaRPr=cp)])

def _skin(ch, title, num_fmt=None, labels=False, label_fmt=None, h=8.0, w_=17.5):
    ch.title=_rt(title)
    ch.height=h; ch.width=w_; ch.style=None
    ch.graphical_properties=GraphicalProperties(noFill=True, ln=LineProperties(noFill=True))
    ch.y_axis.majorGridlines=ChartLines(spPr=GraphicalProperties(
        ln=LineProperties(solidFill=C_GRID, w=6350)))
    ch.x_axis.majorGridlines=None
    for ax in (ch.x_axis, ch.y_axis):
        ax.spPr=GraphicalProperties(ln=LineProperties(solidFill=C_GRID, w=6350))
        ax.txPr=_axis_text()
        ax.majorTickMark="none"; ax.minorTickMark="none"
    ch.y_axis.delete=False; ch.x_axis.delete=False
    ch.layout=Layout(manualLayout=ManualLayout(
        xMode="edge", yMode="edge", x=0.06, y=0.14, w=0.90, h=0.68))
    if num_fmt: ch.y_axis.numFmt=num_fmt
    if ch.legend is not None:
        ch.legend.position="b"          # 底部：与标题彻底分离，杜绝重叠
        ch.legend.overlay=False
        ch.legend.txPr=_axis_text(900, C_TXT)
    if labels:
        ch.dLbls=DataLabelList()
        ch.dLbls.showVal=True; ch.dLbls.showSerName=False; ch.dLbls.showCatName=False
        ch.dLbls.showLegendKey=False; ch.dLbls.showBubbleSize=False
        ch.dLbls.txPr=_axis_text(800, C_TXT)
        if label_fmt: ch.dLbls.numFmt=label_fmt
    return ch

def _bar_series(ref, name, color):
    s=Series(ref, title=name)
    s.graphicalProperties=GraphicalProperties(solidFill=color, ln=LineProperties(noFill=True))
    return s

def _line_series(ref, name, color, marker=True):
    s=Series(ref, title=name); s.smooth=False
    s.graphicalProperties=GraphicalProperties(ln=LineProperties(solidFill=color, w=22000))
    if marker:
        s.marker=Marker(symbol="circle", size=6,
                        spPr=GraphicalProperties(solidFill=color,
                                                 ln=LineProperties(solidFill=color)))
    else:
        s.marker=Marker(symbol="none")
    return s



# ============================================================
# 7_Ratios 主要财务比率 + 杜邦分析 + 图表
# ============================================================
def _ratios(wb, cal, D_):
    M, PR = cal["meta"], cal["periods"]
    R, RF = cal["_rows"], cal["_ref"]
    HC, CC, NH, NY = R["HC"], R["CC"], R["NH"], R["NY"]
    HL, YE = PR["labels"], PR["proj"]
    ALL = HC + CC
    P = "'2_Income'"; B_ = "'3_Balance'"; K = "'4_CashFlow'"; DS = "'5_DCF'"
    RV, RC, RGP, REBIT, RPBT, RTAX, RNI, RMI, RNIP, RSBC = (
        RF[k] for k in ("RV", "RC", "RGP", "REBIT", "RPBT", "RTAX", "RNI", "RMI", "RNIP", "RSBC"))
    w = wb.create_sheet("7_Ratios"); w.sheet_properties.tabColor = "548235"
    w.column_dimensions["A"].width = 36
    for i in range(2, 2 + NH + NY):
        w.column_dimensions[CL(i)].width = 12
    w.cell(1, 1, f"{M['ticker']} — 主要财务比率与杜邦分析（{M.get('unit','百万美元')}）").font = \
        Font(bold=True, size=14, color=NAVY)
    w.cell(2, 1, "全部为活公式，取自三张报表；改「1_Assumptions」本页与下方图表自动重算。"
                 "历史列为实际、预测列为模型输出。").font = Font(size=9, italic=True)

    def hdr(r, title):
        w.cell(r, 1, title).font = F_HDR
        for c in range(1, 2 + NH + NY):
            w.cell(r, c).fill = P_N
        r += 1
        w.cell(r, 1, "指标").font = F_COL; w.cell(r, 1).fill = P_B
        for i, y in enumerate(HL + YE):
            cc = w.cell(r, 2 + i, y); cc.font = F_COL
            cc.alignment = Alignment(horizontal="center")
            cc.fill = P_H if i < NH else P_B
        return r + 1

    def line(r, label, fx, fmt=PCT, bold=False, note=None, fill=None):
        c0 = w.cell(r, 1, label); c0.font = Font(bold=bold, size=10); c0.border = BX
        if note:
            c0.comment = Comment(note, "Template")
        for i, c in enumerate(ALL):
            prev = ALL[i - 1] if i > 0 else None
            f = fx(c, prev, i)
            cc = w.cell(r, 2 + i, f if f else None); cc.number_format = fmt
            cc.font = Font(bold=True, size=10) if bold else (F_H if i < NH else F_FX)
            if fill:
                cc.fill = fill
        return r + 1

    def avg(sheet, row, c, prev):
        return f"AVERAGE({sheet}!{prev}{row},{sheet}!{c}{row})" if prev else f"{sheet}!{c}{row}"
    IE = lambda x: f'=IFERROR({x},"")'
    ROWS = {}
    r = 4

    r = hdr(r, "一、盈利能力 Profitability")
    r = line(r, "Gross margin 毛利率", lambda c, p, i: IE(f"{P}!{c}{RGP}/{P}!{c}{RV}"))
    ROWS["ebitm"] = r
    r = line(r, "EBIT margin 经营利润率", lambda c, p, i: IE(f"{P}!{c}{REBIT}/{P}!{c}{RV}"))
    ROWS["npm"] = r
    r = line(r, "Net margin 归母净利率", lambda c, p, i: IE(f"{P}!{c}{RNIP}/{P}!{c}{RV}"))
    ROWS["sbcm"] = r
    r = line(r, "SBC / Revenue 股份支付占营收", lambda c, p, i: IE(f"{P}!{c}{RSBC}/{P}!{c}{RV}"),
             PCT, False, "美股特有。按 Damodaran 口径它是真实成本，已含在 R&D/SG&A 内，不加回")
    ROWS["roe"] = r
    r = line(r, "ROE（归母 / 平均权益）", lambda c, p, i: IE(f"{P}!{c}{RNIP}/{avg(B_,25,c,p)}"),
             PCT, True, "回购大户的账面权益被冲小，ROE 会失真（AAPL 达 171%）——"
                        "这类公司应以 ROIC 为锚")
    r = line(r, "ROA（净利 / 平均总资产）", lambda c, p, i: IE(f"{P}!{c}{RNI}/{avg(B_,16,c,p)}"))
    ROWS["roic"] = r
    r = line(r, "ROIC（税后 / 生效投入资本）",
             lambda c, p, i: IE(f"{P}!{c}{REBIT}*(1-{DS}!$B${D_['TT']})/{B_}!{c}36"),
             PCT, True, "投入资本用「生效 IC」（含研发资本，若开关打开）")
    ROWS["wacc"] = r
    r = line(r, "WACC（基准线）", lambda c, p, i: f"={DS}!$B${D_['WC']}", PCT, False,
             "ROIC 高于 WACC 才创造价值")
    ROWS["spread"] = r
    r = line(r, "★ 价值利差 ROIC − WACC", lambda c, p, i: IE(f"{c}{ROWS['roic']}-{c}{ROWS['wacc']}"),
             PCT, True, None, P_O)
    r += 1

    r = hdr(r, "二、营运效率 Efficiency")
    ROWS["dso"] = r
    r = line(r, "DSO 应收周转天数", lambda c, p, i: IE(f"{B_}!{c}8/{P}!{c}{RV}*365"), D1)
    r = line(r, "DIO 存货周转天数", lambda c, p, i: IE(f"{B_}!{c}9/{P}!{c}{RC}*365"), D1)
    r = line(r, "DPO 应付周转天数", lambda c, p, i: IE(f"{B_}!{c}20/{P}!{c}{RC}*365"), D1)
    ROWS["ccc"] = r
    r = line(r, "★ 现金转换周期 CCC",
             lambda c, p, i: IE(f"{c}{ROWS['dso']}+{c}{ROWS['dso']+1}-{c}{ROWS['dso']+2}"),
             D1, True, "为负说明供应商在为公司垫资 —— 美股龙头普遍为负")
    ROWS["ato"] = r
    r = line(r, "总资产周转率", lambda c, p, i: IE(f"{P}!{c}{RV}/{avg(B_,16,c,p)}"), MX)
    r = line(r, "固定资产周转率", lambda c, p, i: IE(f"{P}!{c}{RV}/{avg(B_,12,c,p)}"), MX)
    r += 1

    r = hdr(r, "三、偿债与资本结构 Leverage")
    r = line(r, "资产负债率", lambda c, p, i: IE(f"{B_}!{c}24/{B_}!{c}16"))
    r = line(r, "净负债 / 归母权益", lambda c, p, i: IE(f"{B_}!{c}30/{B_}!{c}25"))
    r = line(r, "有息负债 / EBITDA",
             lambda c, p, i: IE(f"({B_}!{c}18+{B_}!{c}19+{B_}!{c}21)"
                                f"/({P}!{c}{REBIT}+{K}!{c}7+{K}!{c}8)"), MX)
    r = line(r, "EBIT 利息保障倍数", lambda c, p, i: IE(f"{P}!{c}{REBIT}/{P}!{c}{RF['RIE']}"), MX)
    ROWS["em"] = r
    r = line(r, "权益乘数（平均总资产 / 平均权益）",
             lambda c, p, i: IE(f"{avg(B_,16,c,p)}/{avg(B_,25,c,p)}"), MX)
    r += 1

    r = hdr(r, "四、现金流质量 Cash quality")
    r = line(r, "经营现金流 / 净利润", lambda c, p, i: IE(f"{K}!{c}15/{P}!{c}{RNI}"), MX)
    r = line(r, "自由现金流 / 营收", lambda c, p, i: IE(f"{K}!{c}18/{P}!{c}{RV}"))
    r = line(r, "资本开支 / 折旧摊销", lambda c, p, i: IE(f"-{K}!{c}16/({K}!{c}7+{K}!{c}8)"), MX,
             False, "长期低于 1 说明在吃老本；远高于 1 说明在扩张期")
    r = line(r, "FCFF / 税后 EBIT",
             lambda c, p, i: IE(f"{K}!{c}19/({P}!{c}{REBIT}*(1-{DS}!$B${D_['TT']}))"), MX)
    ROWS["bb"] = r
    r = line(r, "回购 / 营收", lambda c, p, i: IE(f"-{K}!{c}21/{P}!{c}{RV}"), PCT, False,
             "美股常态：AAPL 近五年稳定在营收的 20%+")
    r += 1

    r = hdr(r, "五、成长性 Growth")
    ROWS["gr"] = r
    r = line(r, "营收增速", lambda c, p, i: IE(f"{P}!{c}{RV}/{P}!{p}{RV}-1") if p else None)
    r = line(r, "归母净利增速", lambda c, p, i: IE(f"{P}!{c}{RNIP}/{P}!{p}{RNIP}-1") if p else None)
    r = line(r, "EBIT 增速", lambda c, p, i: IE(f"{P}!{c}{REBIT}/{P}!{p}{REBIT}-1") if p else None)
    r += 1

    r = hdr(r, "六、杜邦三因子 DuPont (3-step)")
    d3 = r
    r = line(r, "① 归母净利率", lambda c, p, i: IE(f"{P}!{c}{RNIP}/{P}!{c}{RV}"))
    r = line(r, "② 总资产周转率", lambda c, p, i: IE(f"{P}!{c}{RV}/{avg(B_,16,c,p)}"), MX)
    r = line(r, "③ 权益乘数", lambda c, p, i: IE(f"{avg(B_,16,c,p)}/{avg(B_,25,c,p)}"), MX)
    r = line(r, "①×②×③ = ROE", lambda c, p, i: IE(f"{c}{d3}*{c}{d3+1}*{c}{d3+2}"), PCT, True)
    r = line(r, "★ 三因子恒等式校验（应为 0）",
             lambda c, p, i: IE(f"{c}{r-1}-{c}{ROWS['roe']}"), "0.00000000", True,
             "连乘必须等于直接算出的 ROE。这是本页唯一的硬断言 ——"
             "不为 0 说明比率之间的口径打架了", P_O)
    r += 1

    r = hdr(r, "七、杜邦五因子 DuPont (5-step)")
    d5 = r
    r = line(r, "① 税负比率 = 净利 / 税前利润", lambda c, p, i: IE(f"{P}!{c}{RNI}/{P}!{c}{RPBT}"), MX)
    r = line(r, "② 利息负担 = 税前利润 / EBIT", lambda c, p, i: IE(f"{P}!{c}{RPBT}/{P}!{c}{REBIT}"), MX)
    r = line(r, "③ EBIT 利润率", lambda c, p, i: IE(f"{P}!{c}{REBIT}/{P}!{c}{RV}"))
    r = line(r, "④ 总资产周转率", lambda c, p, i: IE(f"{P}!{c}{RV}/{avg(B_,16,c,p)}"), MX)
    r = line(r, "⑤ 权益乘数", lambda c, p, i: IE(f"{avg(B_,16,c,p)}/{avg(B_,25,c,p)}"), MX)
    r = line(r, "⑥ 归母占比 = 归母净利 / 净利润", lambda c, p, i: IE(f"{P}!{c}{RNIP}/{P}!{c}{RNI}"), MX)
    r = line(r, "①×…×⑥ = ROE",
             lambda c, p, i: IE(f"{c}{d5}*{c}{d5+1}*{c}{d5+2}*{c}{d5+3}*{c}{d5+4}*{c}{d5+5}"),
             PCT, True)
    r = line(r, "★ 五因子恒等式校验（应为 0）",
             lambda c, p, i: IE(f"{c}{r-1}-{c}{ROWS['roe']}"), "0.00000000", True, None, P_O)
    r += 3
    _us_charts(w, cal, ROWS, d3, r)


def _us_charts(w, cal, ROWS, d3, start):
    """6 张图。全部引用单元格区域，模型重算后自动跟随。"""
    R = cal["_rows"]; NH, NY = R["NH"], R["NY"]; n = NH + NY
    CATS = Reference(w, min_col=2, max_col=1 + n, min_row=5, max_row=5)
    wi = w.parent["2_Income"]; RF = cal["_ref"]
    ref = lambda sheet, row: Reference(sheet, min_col=2, max_col=1 + n, min_row=row, max_row=row)
    w.cell(start - 1, 1, "图表引用单元格区域，模型重算后自动更新").font = \
        Font(size=9, italic=True, color=GREY)

    bar = BarChart(); bar.type = "col"; bar.gapWidth = 60
    bar.series.append(_bar_series(ref(wi, RF["RV"]), "Revenue 营业收入", C_RED))
    bar.set_categories(CATS)
    _skin(bar, f"营业收入与归母净利润（{cal['meta'].get('unit','百万美元')}）", labels=True, label_fmt="#,##0")
    ln = LineChart()
    ln.series.append(_line_series(ref(wi, RF["RNIP"]), "Net income 归母净利", C_NAVY))
    ln.y_axis.axId = 200; ln.y_axis.crosses = "max"; ln.y_axis.majorGridlines = None
    ln.y_axis.spPr = GraphicalProperties(ln=LineProperties(solidFill=C_GRID, w=6350))
    ln.y_axis.txPr = _axis_text()
    bar += ln
    w.add_chart(bar, f"A{start}")

    ch = LineChart()
    for row, name, col in [(ROWS["ebitm"] - 1, "毛利率", C_RED),
                           (ROWS["ebitm"], "经营利润率", C_KHAKI),
                           (ROWS["npm"], "归母净利率", C_NAVY),
                           (ROWS["sbcm"], "SBC/营收", C_SLATE)]:
        ch.series.append(_line_series(ref(w, row), name, col))
    ch.set_categories(CATS); _skin(ch, "盈利能力趋势（含股份支付占比）", num_fmt="0%")
    w.add_chart(ch, f"K{start}")

    ch = LineChart()
    ch.series.append(_line_series(ref(w, ROWS["roe"]), "ROE（归母）", C_RED))
    ch.series.append(_line_series(ref(w, ROWS["roic"]), "ROIC（税后）", C_NAVY))
    s = _line_series(ref(w, ROWS["wacc"]), "WACC（基准线）", C_GREY, marker=False)
    s.graphicalProperties.ln.dashStyle = "dash"
    ch.series.append(s)
    ch.set_categories(CATS)
    _skin(ch, "价值创造：ROIC 高于 WACC 才创造价值", num_fmt="0%")
    w.add_chart(ch, f"A{start + 17}")

    ch = LineChart()
    ch.series.append(_line_series(ref(w, d3 + 1), "总资产周转率", C_KHAKI))
    ch.series.append(_line_series(ref(w, d3 + 2), "权益乘数", C_NAVY))
    ch.set_categories(CATS)
    _skin(ch, "杜邦：总资产周转率与权益乘数", num_fmt="0.00", labels=True, label_fmt="0.00")
    w.add_chart(ch, f"K{start + 17}")

    ch = LineChart()
    ch.series.append(_line_series(ref(w, ROWS["ccc"]), "现金转换周期 CCC（天）", C_CORAL))
    ch.set_categories(CATS)
    _skin(ch, "现金转换周期：为负说明供应商在垫资", num_fmt="0.0", labels=True, label_fmt="0")
    w.add_chart(ch, f"A{start + 34}")

    ch = LineChart()
    ch.series.append(_line_series(ref(w, ROWS["bb"]), "回购 / 营收", C_RED))
    ch.series.append(_line_series(ref(w, ROWS["sbcm"]), "SBC / 营收", C_SLATE))
    ch.set_categories(CATS)
    _skin(ch, "资本返还与股份支付（占营收）", num_fmt="0%")
    w.add_chart(ch, f"K{start + 34}")
