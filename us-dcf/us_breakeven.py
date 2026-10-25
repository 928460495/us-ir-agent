# -*- coding: utf-8 -*-
"""盈亏平衡敏感性（FINC6021 Ch.3 讲义 p.22–25）。

课程问的是「令 NPV = 0 的临界值」。股票估值里的对应问题是
**「令每股内在价值 = 现价 的临界值」** —— 也就是：我要错到什么程度，结论才会翻转。

这与龙卷风图回答的不是同一个问题：

    龙卷风   = 在我假设的不确定性范围内，这个变量能让价值波动多少   →【离散度】
    盈亏平衡 = 我要错到什么程度，结论才会翻转                    →【脆弱度】

一个变量可以龙卷风条很长但临界值很远（结论稳健），也可以条很短但临界值近在咫尺
（结论脆弱）。课程的 Earthilizer 案例里，6 个变量中只有 3 个的临界偏离在 15% 以内
（毛利率 −4.54%、销售费用率 +14.80%、基期销售 −7.68%），研究精力就该压在这三个上。

实现：在 Python 侧对 us_verify.recompute 做二分求解。讲义推荐用 Excel 的 Goal Seek，
两者是同一件事；用 Python 的好处是能一次跑完所有驱动因子，且结果可复现。
"""
from __future__ import annotations
import copy
import numpy as np
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

import us_verify as V
import valuation_lib as VL

NAVY = "1F3864"; RED = "C00000"; GREY = "808080"
F_HDR = Font(bold=True, size=11, color="FFFFFF")
F_COL = Font(bold=True, size=10, color="FFFFFF")
P_N = PatternFill("solid", fgColor=NAVY)
P_B = PatternFill("solid", fgColor="2E75B6")
P_O = PatternFill("solid", fgColor="FCE4D6")
P_R = PatternFill("solid", fgColor="FFC7CE")     # 红底 = 极脆弱
P_Y = PatternFill("solid", fgColor="FFF2CC")
BX = Border(bottom=Side(style="thin", color="D9D9D9"))
PCT = "0.0%"; PCT2 = "0.00%"; USD = '"$"#,##0.00'; D1 = "0.0"; MX = "0.000"


# ---- 驱动因子定义 -------------------------------------------------
# 每项: (显示名, 单位类型, 取期望值, 施加新值, 搜索区间)
#   unit: "pct" 百分比 / "days" 天数 / "raw" 纯数
def _path_mean(xs):
    return float(np.mean(xs))


def _shift_path(inp, key, new_mean):
    """路径类变量：整体平移到新的均值，保持年际形状不变。"""
    cur = _path_mean(inp[key])
    d = new_mean - cur
    inp[key] = [x + d for x in inp[key]]


DRIVERS = [
    ("营收增速（全期均值）", "pct", "path", "growth",
     lambda i: _path_mean(i["growth"]), (-0.60, 1.50)),
    ("毛利率（全期均值）", "pct", "path", "gm",
     lambda i: _path_mean(i["gm"]), (0.01, 0.95)),
    ("资本开支 / 营收（全期均值）", "pct", "path", "capex",
     lambda i: _path_mean(i["capex"]), (0.0, 0.90)),
    ("研发费用 / 营收", "pct", "gen", "研发费用 / 营收", None, (0.0, 0.90)),
    ("销售及管理费用 / 营收", "pct", "gen", "销售及管理费用 / 营收", None, (0.0, 0.90)),
    ("有效所得税率", "pct", "gen", "有效所得税率", None, (0.0, 0.95)),
    # ⚠ 折旧率在本模型里只进 FCFF 加回与 PP&E 滚动，【不进 EBIT】——
    # EBIT 由毛利率与费用率驱动，历史值里已含折旧，但改折旧率并不会改 EBIT。
    # 因此它的临界值在经济上没有意义（调高折旧率会凭空抬高 FCFF）。保留但标注。
    ("折旧率（占期初 PP&E）", "pct", "gen", "折旧率（占期初固定资产净额）", None, (0.0, 3.0)),
    ("应收周转天数 DSO", "days", "gen", "应收周转天数 DSO", None, (0.0, 3000.0)),
    ("应付周转天数 DPO", "days", "gen", "应付周转天数 DPO", None, (0.0, 3000.0)),
    ("Beta（→ Ke → WACC）", "raw", "dcf", "Beta（5年对标普500回归+Blume）", None, (0.05, 12.0)),
    ("股权风险溢价 ERP", "pct", "dcf", "股权风险溢价 ERP", None, (0.001, 0.60)),
    # g 的区间必须【按公司动态算】：g ≥ WACC 时 Gordon 分母为负，g/ROIC ≥ 1 时再投资率越界，
    # 两种情况都会触发终值降级 → 函数在两端被压平成同一个值（实测 MSFT 两端都是 $251.3），
    # 二分因此永远不变号。固定宽区间在这里是错的，不是保守。
    ("永续增长率 g", "pct", "dcf", "永续增长率 g", None, "g_bounds"),
    ("终值所得税率 t_term", "pct", "dcf", "终值所得税率 t_term", None, (0.0, 0.95)),
]


def _g_bounds(inp) -> tuple[float, float]:
    """g 的有效区间：下界给一个温和的负增长，上界取 min(WACC, ROIC) 再留安全边距。
    超出这个区间，终值会降级，函数变成常数，求解无意义。"""
    _rows, dv = V.recompute(inp)
    hi = min(dv["wacc"], dv["roic"] if np.isfinite(dv["roic"]) and dv["roic"] > 0 else dv["wacc"])
    return (-0.05, max(-0.04, hi * 0.98 - 1e-4))


def _apply(inp, kind, key, val):
    out = copy.deepcopy(inp)
    if kind == "path":
        _shift_path(out, key, val)
    else:
        out[kind][key] = val
    return out


def _ps(inp) -> float:
    try:
        _rows, dv = V.recompute(inp)
        return float(dv["ps"])
    except Exception:
        return float("nan")


def compute(path: str) -> dict:
    inp = V.read_inputs(path)
    base_ps = _ps(inp)
    price = inp["o"]["现价（美元）"]
    rows = []
    for name, unit, kind, key, getter, bounds in DRIVERS:
        lo, hi = _g_bounds(inp) if bounds == "g_bounds" else bounds
        exp = getter(inp) if getter else float(inp[kind][key])
        crit = VL.breakeven(lambda x: _ps(_apply(inp, kind, key, x)), lo, hi, price)
        if not np.isfinite(crit):
            fa, fb = _ps(_apply(inp, kind, key, lo)), _ps(_apply(inp, kind, key, hi))
            best = max(fa, fb) if np.isfinite(fa) and np.isfinite(fb) else float("nan")
            rows.append(dict(变量=name, 单位=unit, 期望值=exp, 临界值=None, 偏离=None,
                             绝对变动=None,
                             说明=(f"区间 [{lo:g}, {hi:g}] 内最高只能到 ${best:,.0f}，"
                                   f"够不着现价 ${price:,.0f} —— 单独调这一个变量翻转不了结论"
                                   if np.isfinite(best) else
                                   f"在 [{lo:g}, {hi:g}] 内不变号")))
            continue
        pct = (crit - exp) / abs(exp) if abs(exp) > 1e-12 else float("nan")
        warn = ("⚠ 折旧率只进 FCFF 加回与 PP&E 滚动，不进 EBIT，"
                "调高它会凭空抬高 FCFF —— 该临界值无经济含义，仅列出备查"
                if "折旧率" in name else "")
        rows.append(dict(变量=name, 单位=unit, 期望值=exp, 临界值=crit,
                         偏离=pct if np.isfinite(pct) else None,
                         绝对变动=crit - exp, 说明=warn))
    solved = [r for r in rows if r["偏离"] is not None]
    unsolved = [r for r in rows if r["偏离"] is None]
    solved.sort(key=lambda r: abs(r["偏离"]))
    return dict(base_ps=base_ps, price=price, rows=solved + unsolved,
                n_solved=len(solved))


def build_sheet(wb, res: dict, dcf_ps_ref: str) -> None:
    if "8_Breakeven" in wb.sheetnames:
        del wb["8_Breakeven"]
    w = wb.create_sheet("8_Breakeven"); w.sheet_properties.tabColor = "7F7F7F"
    w.column_dimensions["A"].width = 34
    for c in "BCDEF":
        w.column_dimensions[c].width = 15
    w.column_dimensions["G"].width = 74
    w.cell(1, 1, "盈亏平衡敏感性 —— 我要错到什么程度，结论才会翻转？").font = Font(bold=True, size=14, color=NAVY)
    w.cell(2, 1, "FINC6021 Ch.3 讲义 p.22–25。课程问的是「令 NPV=0 的临界值」；"
                 "股票估值的对应问题是「令每股内在价值 = 现价 的临界值」。"
           ).font = Font(size=9, italic=True, color=GREY)
    w.cell(3, 1, "⚠ 本页为【静态值】，由 Python 二分求解写入。改「1_Assumptions」后需重跑 us_dcf.py 才会更新 —— "
                 "下方设有过期检测。").font = Font(size=9, color=RED)

    r = 5
    w.cell(r, 1, "基准").font = F_HDR
    for c in range(1, 8):
        w.cell(r, c).fill = P_N
    r += 1
    w.cell(r, 1, "生成时的每股内在价值").font = Font(size=10)
    cc = w.cell(r, 2, round(res["base_ps"], 6)); cc.number_format = USD; r += 1
    w.cell(r, 1, "当前每股内在价值（活链接）").font = Font(size=10)
    cc = w.cell(r, 2, dcf_ps_ref); cc.number_format = USD; cc.font = Font(size=10, color="008000")
    live = r; r += 1
    w.cell(r, 1, "现价（目标值）").font = Font(size=10)
    cc = w.cell(r, 2, round(res["price"], 6)); cc.number_format = USD; r += 1
    w.cell(r, 1, "★ 过期检测").font = Font(bold=True, size=10)
    cc = w.cell(r, 2, f'=IF(ABS(B{live}-B{live-1})<0.005,"✓ 与生成时一致","⚠ 假设已变更，本页数字已过期，请重跑")')
    cc.font = Font(bold=True, size=10, color=RED); cc.fill = P_O
    w.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4); r += 2

    w.cell(r, 1, "各驱动因子的临界值（按脆弱度排序：偏离越小越关键）").font = F_HDR
    for c in range(1, 8):
        w.cell(r, c).fill = P_N
    r += 1
    for i, h in enumerate(["驱动因子", "期望值", "临界值", "绝对变动", "偏离 %", "方向", "说明"]):
        cc = w.cell(r, 1 + i, h); cc.font = F_COL; cc.fill = P_B
        cc.alignment = Alignment(horizontal="center")
    r += 1
    for row in res["rows"]:
        w.cell(r, 1, row["变量"]).font = Font(size=10); w.cell(r, 1).border = BX
        fmt = {"pct": PCT2, "days": D1, "raw": MX}[row["单位"]]
        c2 = w.cell(r, 2, round(row["期望值"], 6)); c2.number_format = fmt
        if row["临界值"] is None:
            w.cell(r, 3, "—"); w.cell(r, 7, row["说明"]).font = Font(size=9, color=GREY)
            r += 1
            continue
        c3 = w.cell(r, 3, round(row["临界值"], 6)); c3.number_format = fmt
        c4 = w.cell(r, 4, round(row["绝对变动"], 6)); c4.number_format = fmt
        c5 = w.cell(r, 5, round(row["偏离"], 6)); c5.number_format = PCT
        c6 = w.cell(r, 6, "需上升" if row["绝对变动"] > 0 else "需下降")
        c6.alignment = Alignment(horizontal="center")
        a = abs(row["偏离"])
        if a < 0.10:
            for c in range(1, 7):
                w.cell(r, c).fill = P_R
            w.cell(r, 7, "⚠ 极脆弱：不到 10% 的偏差就足以翻转结论").font = Font(size=9, color=RED, bold=True)
        elif a < 0.25:
            for c in range(1, 7):
                w.cell(r, c).fill = P_Y
            w.cell(r, 7, "关键：研究精力应优先投在这里").font = Font(size=9, color="BF8F00")
        r += 1
    r += 1

    w.cell(r, 1, "怎么读这张表").font = F_HDR
    for c in range(1, 8):
        w.cell(r, c).fill = P_N
    r += 1
    for t in [
        "· 「偏离 %」= (临界值 − 期望值) / |期望值|，即该变量要变动多少才能让内在价值等于现价。",
        "· 排在最上面的最关键 —— 不是因为它波动大，而是因为【结论对它最脆弱】。",
        "· 这与龙卷风图问的不是同一个问题：龙卷风量的是离散度（能摆多少），本页量的是脆弱度（要错多少才翻转）。",
        "   一个变量可以龙卷风条很长但临界值很远（结论稳健），也可以条很短但临界值近在咫尺（结论脆弱）。",
        "· 讲义明确的两条局限：① 一次只动一个变量，而驱动因子之间往往相关"
        "（降资本开支会拖累增长、提价会压销量）；② 不知道超出临界值的【概率】。",
        "· 显示「不变号」的变量，意味着单独调它无论调到区间的哪一端都无法把估值推到现价 —— "
        "结论对它不敏感，或者需要多个变量同时变化。",
        "· 讲义推荐用 Excel 的 Goal Seek 做同一件事；本页用 Python 二分一次算完，结果可复现。",
    ]:
        w.cell(r, 1, t).font = Font(size=9, color="404040")
        w.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7); r += 1
    w.freeze_panes = "A5"


def add(path: str, dcf_ps_row: int) -> dict:
    from openpyxl import load_workbook
    res = compute(path)
    wb = load_workbook(path)
    build_sheet(wb, res, f"='5_DCF'!B{dcf_ps_row}")
    wb.save(path)
    return res


# ============================================================
# 增速衰减期敏感性
# ============================================================
def fade_sensitivity(path: str) -> dict:
    """增速衰减期是这个模型【最被低估的主导假设】。

    它曾经与明确期年数耦合（`_fade(g0, g_term, n)` 里的 n 就是明确期），
    于是 `--years 5 → 10` 不只是多建模 5 年，而是把衰减期整整拉长一倍 ——
    实测 NVDA 因此从 −39% 翻到 +121%，一个开关让结论完全反转且无任何提示。
    解耦之后明确期基本价值中性（AAPL −55% → −56%），衰减期成为独立的判断项。

    既然它主导结果，就必须让使用者【看见】它，而不是藏在 CLI 参数里。"""
    import copy
    inp = V.read_inputs(path)
    price = inp["o"]["现价（美元）"]
    n = inp["ny"]
    g0 = inp["growth"][0]
    gt = inp["dcf"]["永续增长率 g"]
    cur = _ps(inp)
    rows = []
    for f in range(2, n + 1):
        i2 = copy.deepcopy(inp)
        if f <= 1:
            i2["growth"] = [gt] * n
        else:
            head = [round(g0 + (gt - g0) * i / (f - 1), 6) for i in range(f)]
            i2["growth"] = head + [gt] * (n - f)
        ps = _ps(i2)
        rows.append(dict(fade=f, ps=ps, upside=ps / price - 1 if price else None,
                         mean_g=sum(i2["growth"]) / n))
    return dict(current=cur, price=price, n=n, rows=rows,
                note=("衰减期不能超过明确期 —— 若你认为竞争优势可维持更久，"
                      "需同时用 --years 拉长明确期，否则终值会在增速仍高时就接管"))
