#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""美股 DCF 通用模板 — CLI

用法:
    python3 us_dcf.py AAPL
    python3 us_dcf.py NVDA --years 8 --out ~/Desktop

数据源：SEC EDGAR XBRL（主）+ FMP（交叉校验）+ yfinance（行情/拆股）+ FRED（利率/利差）
方法：Titman & Martin《Valuation》/ FINC6021 + Damodaran 终值一致性
"""
from __future__ import annotations
import argparse, os, sys, datetime as dt, warnings
warnings.filterwarnings("ignore")


def main() -> int:
    ap = argparse.ArgumentParser(description="输入美股代码，生成可编辑的三表 + DCF 模型")
    ap.add_argument("ticker", help="美股代码，如 AAPL")
    ap.add_argument("--years", type=int, default=5, help="明确期年数（建模粒度，默认 5）")
    ap.add_argument("--fade-years", type=int, default=None,
                    help="增速衰减至永续增速所需年数（核心假设，默认 min(years,5)）。"
                         "与 --years 已解耦：改 --years 不再改变增速路径的形状")
    ap.add_argument("--out", default="models", help="输出目录")
    ap.add_argument("--no-verify", action="store_true", help="跳过公式引擎验算（不推荐）")
    ap.add_argument("--mc", action="store_true", help="附加蒙特卡洛页")
    ap.add_argument("--mc-paths", type=int, default=None, help="模拟路径数（默认 50000）")
    a = ap.parse_args()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import us_data as D, us_calibrate as C, us_build as B

    try:
        print(f"[1/4] 取数 {a.ticker.upper()} …")
        cal = C.calibrate(a.ticker, a.years, a.fade_years)
    except D.DataError as e:
        print(f"\n❌ {e}", file=sys.stderr); return 2

    M, CO, dg = cal["meta"], cal["course"], cal["meta"]["diag"]
    print(f"      {M['name']}  SIC {M['sic']} {M['sic_desc']}  财年末 {M['fye']}")
    print(f"      现价 ${M['price']:.2f}  股数 {M['shares']:,.0f}mn  市值 ${M['mktcap']/1000:,.1f}B  行情源 {M['quote_source']}")

    print(f"[2/4] 校准假设 …")
    print(f"      Rf {M['rf']*100:.2f}%（{M['rf_date']}）  β 5年 {M['beta_raw']}→{M['beta']} ｜ 2年 {M['beta2_raw']}→{M['beta2']}")
    print(f"      Altman Z {CO['z']} → {CO['zone']} / 合成评级 {CO['rating']}（D 项占 {CO['z_d_share']:.0%}）"
          f"  利差 {CO['spread']*100:.2f}%")
    print(f"      期初平衡 {cal['open']['_check']:.1e}   滚动年数 {cal['dcf']['roll']}")

    # 双源交叉校验
    print(f"[3/4] EDGAR ↔ FMP 交叉校验 …")
    g = D.facts(M["cik"]); st = D.statements(g, M["ticker"], a.years)
    cc = D.cross_check(M["ticker"], st)
    if cc["ok"] is None:
        print(f"      ⚠ {cc['note']}")
    elif cc["ok"]:
        print(f"      ✅ {cc['note']}")
    else:
        print(f"      ⚠ {cc['note']}：")
        for x in cc["rows"][:8]:
            print(f"        · {x}")
    for k in ("缺失", "期间不全", "跨标签补缺", "换源", "推导", "警告",
              "倒挤项异动（请人工确认不是丢标签）"):
        if dg.get(k):
            items = dg[k] if k != "缺失" else ["、".join(dg[k])]
            for it in items[:6]:
                print(f"      〔{k}〕{it}")

    os.makedirs(a.out, exist_ok=True)
    fn = os.path.join(a.out, f"{M['ticker']}_{dt.date.today():%Y%m%d}_DCF.xlsx")
    B.build(cal, fn)

    if a.no_verify:
        print(f"[4/4] 已跳过验算\n\n✅ {fn}"); return 0
    print(f"[4/4] 公式引擎实跑验算 …")
    import us_verify as V
    fails, dv = V.verify(fn)
    extra = [x for x in V.verify_all_scenarios(fn, quiet=True) if x[0] != "Base"]
    fails += extra
    if fails:
        print(f"\n❌ 验算未通过，文件可能有误：{fn}", file=sys.stderr)
        for f in fails[:10]:
            print("   ✗", f, file=sys.stderr)
        return 1

    print(f"\n✅ {fn}")
    print(f"   每股内在价值 ${dv['ps']:.2f}   现价 ${M['price']:.2f}   空间 {(dv['ps']/M['price']-1)*100:+.1f}%")
    print(f"   WACC {dv['wacc']*100:.2f}%（Ke {dv['ke']*100:.2f}% / Kd {dv['kd']*100:.2f}% / Wd {cal['dcf']['wd']*100:.2f}%）")
    print(f"   终值 ROIC {dv['roic']*100:.1f}%  再投资率 {dv['rr']*100:.1f}%  终值占 EV {dv['tw']*100:.1f}%"
          + ("  ⚠ 终值已降级" if dv["degrade"] else ""))
    rows, _ = V.recompute(V.read_inputs(fn))
    for wmsg in V.sanity(dv, rows):
        print(f"   ⚠ {wmsg}")
    # 盈亏平衡敏感性：在验算通过之后追加（写的是静态值，不影响引擎）
    import us_breakeven as BE
    from us_verify import _rowmap, _find
    from openpyxl import load_workbook as _lw
    ps_row = _find(_rowmap(_lw(fn)["5_DCF"]), "每股内在价值")
    res = BE.add(fn, ps_row)
    print(f"\n   盈亏平衡敏感性（令内在价值 = 现价 的临界值，{res['n_solved']}/{len(res['rows'])} 个变量有解）：")
    for x in res["rows"][:5]:
        if x["偏离"] is None:
            continue
        u = {"pct": lambda v: f"{v:.2%}", "days": lambda v: f"{v:.1f}天",
             "raw": lambda v: f"{v:.3f}"}[x["单位"]]
        print(f"     · {x['变量']:26s} {u(x['期望值']):>10s} → {u(x['临界值']):>10s}"
              f"   偏离 {x['偏离']:+7.1%}")
    fs = BE.fade_sensitivity(fn)
    print(f"\n   增速衰减期敏感性（衰减期 = 增速从当前降到永续增速所需年数）：")
    for x in fs["rows"]:
        mark = "  ← 当前" if x["fade"] == cal["judg"]["fade_years"] else ""
        print(f"     · {x['fade']:2d} 年   全期均增速 {x['mean_g']:>6.1%}   "
              f"每股 ${x['ps']:>8.2f}   空间 {x['upside']:>+7.1%}{mark}")
    print(f"     ⚠ {fs['note']}")
    print(f"\n   ⚠ 营收增速/毛利率/资本开支/SBC 为判断项（黄底），三档初始同值，请自行复核并拉开")
    print(f"   ⚠ 默认增速路径 = 从最近一年实际增速线性衰减至永续增速，这是【起点】不是预测")
    if CO["g_sust_usable"]:
        print(f"   〔交叉校验〕可持续增长 g =（1−派息率）×ROE = {CO['g_sustainable']:.1%}"
              f"，对照终值 g {cal['dcf']['g']:.1%}")
    else:
        print(f"   〔交叉校验〕可持续增长率不可用：{CO['g_sust_note']}")
    if a.mc:
        import us_mc as MC
        j = cal["judg"]
        hist = dict(sd_growth=j["g_stats"][1], sd_margin=j["gm_stats"][1],
                    rho=cal.get("_rho", 0.0), rho_n=cal.get("_rho_n", 0),
                    tax_mean=cal["gen"]["tax"], beta_mean=cal["dcf"]["beta"])
        MC.run(fn, hist=hist, n=a.mc_paths)
    return 0


if __name__ == "__main__":
    sys.exit(main())
