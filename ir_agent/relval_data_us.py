"""美股相对估值取数（设计「四.3」，计划 Task 7）。与 `relval_data.gather` 同形状的返回值。

**同业按期末日期对齐本司，不按标签。** 苹果 9 月、戴尔 1 月财年，同名「2026Q1-Q3」差四个月。
三个截止期 —— 利润 TTM、净资产余额、EBITDA 流量 —— 各自用 `us_align.align_quarter` 在同业
自己的标签上挑「不晚于本司截止季、最多落后一季」的那一期；落后一季的写进 `lags`，页面逐家标出
（A 股「同业没出本季就用上一季」同一规则）。基本面年度期用 `align_fy` 挑期末最近的财年。

**同业数据不重取。** `_load_industry_us` 已把全 FF49 同业的 companyfacts（`r.peer_books`）与
IEX 行情（`r.peer_quotes`）挂在 r 上；这里只组装观测。回归样本是全 FF49（不是行业指标用的
收窄样本）；可比是 `r.comps_codes` 那几家 —— **同一份观测**。

**NTM 只给本司与可比**（用户决定 D3）：FMP 一致预期逐只请求，免费档一天 250 次、标的范围受限。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ir_agent.ledger import FactLedger, LookAheadError
from ir_agent.relval import PeerObs
from ir_agent.relval_data import (_Q_OF, RelvalUnavailable, _label, _prepare, balance_period,
                                  flow_end, flow_ttm, fund_period, obs_from_ledger)


def ntm_eps(cons, fye_mmdd: str, as_of: date) -> tuple[Decimal, str] | None:
    """NTM EPS = w × E[本财年] + (1 − w) × E[下一财年]，w = 本财年剩余天数 ÷ 365（截到 0–1）。
    缺任一财年的预期返回 None —— 拿一个财年顶两个会把 NTM 说成 FY。"""
    from ir_agent.sources.us_periods import _nominal_fye, _parse_fye, fiscal_year

    if cons is None or not getattr(cons, "eps", None):
        return None
    fy0 = fiscal_year(as_of, fye_mmdd)
    e0, e1 = cons.eps.get(fy0), cons.eps.get(fy0 + 1)
    if e0 is None or e1 is None:
        return None
    m, d = _parse_fye(fye_mmdd)
    days = (_nominal_fye(fy0, m, d) - as_of).days
    w = min(max(Decimal(days) / Decimal(365), Decimal(0)), Decimal(1))
    v = w * e0.median + (1 - w) * e1.median
    return v, f"NTM：FY{fy0} × {w:.3f} + FY{fy0 + 1} × {1 - w:.3f}（{cons.stat or '均值'}，FMP）"


def _visible(facts, key: str, as_of: date) -> set[str]:
    return {f.period for f in facts if f.key == key and f.as_of <= as_of}


def _q_of(label: str) -> tuple[int, int]:
    return int(label[:4]), _Q_OF[label[4:]]


def _ntm_for(ticker: str, fye: str, as_of: date, store) -> tuple[Decimal | None, str]:
    from ir_agent.sources import fmp

    try:
        cons = fmp.fetch_consensus(ticker, fye, as_of, store)
    except Exception:                                       # noqa: BLE001
        cons = None
    got = ntm_eps(cons, fye, as_of)
    return (got[0], got[1]) if got else (None, "")


def gather_us(r, as_of: date, snapshot_dir) -> tuple[PeerObs, list[PeerObs], list[PeerObs],
                                                     Decimal, Decimal, list[str]]:
    from ir_agent.operators.ttm import latest_visible
    from ir_agent.sources.us_quotes import peer_market_cap
    from ir_agent.us_align import align_fy, align_quarter

    books = getattr(r, "peer_books", None)
    quotes = getattr(r, "peer_quotes", None)
    peers = getattr(r, "peer_set", None)
    if not books or peers is None:
        raise RelvalUnavailable("行业样本未取到（第一阶段行业聚合失败或未运行）")
    if not quotes:
        raise RelvalUnavailable("同业行情未取到（Tiingo IEX 批量不可用），无从算倍数")
    ends = getattr(r, "target_ends", None) or {}

    mc = next((f for f in (r.quotes.facts if r.quotes else []) if f.key == "market_cap"), None)
    try:
        price = r.ledger.get("last_price", mc.period, as_of=as_of).value if mc else None
    except (KeyError, LookAheadError):
        price = None
    if mc is None or not mc.value or not price:
        raise RelvalUnavailable("本司市值或现价缺失")
    qday = mc.as_of

    # 本司三个截止期（标签 → 期末日）；同业对齐它们
    fp = fund_period(r.period)
    fy_end = ends.get(fp)
    np_f = latest_visible(r.ledger, "net_profit_ttm", as_of)
    np_end = ends.get(np_f.period) if np_f else None
    bs_label = balance_period(r.ledger, as_of)
    bs_end = ends.get(bs_label) if bs_label else None
    fe = flow_end(r.ledger, as_of)
    fl_end = ends.get(_label(fe)) if fe else None
    if fy_end is None:
        raise RelvalUnavailable(f"本司 {fp} 的期末日取不到，无从对齐同业")

    store = getattr(r, "store", None)
    fye = (getattr(r, "sec_meta", None) or {}).get("fye", "")
    tcode = peers.target
    target = obs_from_ledger(tcode, peers.target_name, r.ledger, fp, as_of, mc.value,
                             bs_period=bs_label, flow_end_q=fe)
    if np_end is not None:
        target.np_basis = f"TTM 截至 {np_end.isoformat()}"
    target.price = price
    target.eps_ntm, target.ntm_basis = _ntm_for(tcode, fye, as_of, store)

    comps_codes = list(getattr(r, "comps_codes", None) or [])
    notes: list[str] = []
    sample: list[PeerObs] = []
    for t, b in books.items():
        if t == tcode:
            continue
        q = quotes.get(t)
        if q is None:
            notes.append(f"{t} {b.name}：无行情（Tiingo IEX 未返回），剔除")
            continue
        if q.as_of != qday:
            notes.append(f"{t} {b.name}：行情停在 {q.as_of.isoformat()}，与本司报价日 "
                         f"{qday.isoformat()} 不同（可能停牌），剔除")
            continue
        cap, why = peer_market_cap(b.facts, q, as_of)
        if cap is None:
            notes.append(f"{t} {b.name}：{why}剔除")
            continue
        peer_fp = align_fy(b.ends, fy_end, _visible(b.facts, "revenue", as_of)
                           & _visible(b.facts, "equity_attr_parent", as_of))
        if peer_fp is None:
            notes.append(f"{t} {b.name}：截至估值时点没有与本司 {fp}（期末 {fy_end.isoformat()}）"
                         "对齐的年报，剔除（不用更早的年报凑）")
            continue
        led = FactLedger()
        for f in b.facts:
            led.put(f)
        _prepare(led, peer_fp, as_of)

        lags: dict[str, str] = {}
        np_ttm = None
        if np_end is not None:
            hit = align_quarter(b.ends, np_end, _visible(b.facts, "net_profit_attr_parent", as_of))
            if hit:
                v = flow_ttm(led, "net_profit_attr_parent", _q_of(hit[0]), as_of)
                if v is not None:
                    np_ttm = (v, f"TTM 截至 {b.ends[hit[0]].isoformat()}")
                    if hit[1]:
                        lags["np"] = f"利润 TTM 截至 {b.ends[hit[0]].isoformat()}，早一季"
        bs_p = None
        if bs_end is not None:
            hit = align_quarter(b.ends, bs_end, _visible(b.facts, "equity_attr_parent", as_of))
            if hit:
                bs_p = hit[0]
                if hit[1]:
                    lags["bs"] = f"净资产截至 {b.ends[hit[0]].isoformat()}，早一季"
        fl_q = None
        if fl_end is not None:
            hit = align_quarter(b.ends, fl_end, _visible(b.facts, "operating_profit", as_of))
            fl_q = _q_of(hit[0]) if hit else None

        o = obs_from_ledger(t, b.name, led, peer_fp, as_of, cap, np_ttm=np_ttm,
                            bs_period=bs_p, flow_end_q=fl_q)
        o.lags = lags
        if t in comps_codes:
            o.price = q.price
            o.eps_ntm, o.ntm_basis = _ntm_for(t, b.fye, as_of, store)
        sample.append(o)

    by_code = {o.code: o for o in sample}
    notes += [f"可比 {c}：未进入样本（原因见上），可比法少一家" for c in comps_codes if c not in by_code]
    notes += list(getattr(r, "peer_notes", None) or [])
    comps = [by_code[c] for c in comps_codes if c in by_code]
    return target, sample, comps, mc.value / price, price, notes
