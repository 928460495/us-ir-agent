"""美股估值层的供数：让 us-dcf 与分析层用**同一份**数据。

A 股（`dcf_data.to_frames`）把账本铺成 ashare-dcf 的报表。美股不这么做：us-dcf 的报表要约
四十个科目，外加资产负债表倒挤、折旧摊销拆分、夹层权益、拆股还原等推导，账本只带其中一部分，
在这边重写一遍等于复制 us-dcf 已测试的抽取逻辑，两套迟早分叉。

做法：pipeline 已落快照的**同一份 companyfacts**，按分析时点过滤掉之后才申报的条目，交给 us-dcf
自己抽取；再把报表与账本的核心科目逐年对账，差异进告警（两层选标签的规则不同，可能有出入）。
行情也由 ir-agent 供给：现价取账本（FMP → Tiingo），日线取 Tiingo，标普 500 取 FRED。
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from ir_agent.ledger import FactLedger, LookAheadError
from ir_agent.sources.us_periods import fiscal_label

US_DCF_DIR = Path(os.environ.get("US_DCF_DIR") or Path(__file__).resolve().parent.parent / "us-dcf")
MN = Decimal(10) ** 6
RECONCILE_TOL = Decimal("0.005")       # 0.5%：舍入与标签口径的正常差异之内
# us-dcf 报表科目 ↔ 账本键（对账用的核心科目）
CORE = (("income", "营业收入", "revenue"), ("income", "经营利润", "operating_profit"),
        ("income", "归母净利润", "net_profit_attr_parent"),
        ("balance", "资产总计", "total_assets"), ("balance", "归母权益", "equity_attr_parent"))


def pit_us_gaap(doc: dict, as_of: date) -> dict:
    """companyfacts 的 us-gaap 部分，只留 as_of 之前申报的条目（时点守卫）。"""
    cut = as_of.isoformat()
    out = {}
    for tag, node in (doc.get("facts", {}).get("us-gaap") or {}).items():
        units = {u: [x for x in arr if (x.get("filed") or "9999") <= cut]
                 for u, arr in (node.get("units") or {}).items()}
        units = {u: arr for u, arr in units.items() if arr}
        if units:
            out[tag] = {**{k: v for k, v in node.items() if k != "units"}, "units": units}
    return out


def splits_from_tiingo(rows: list[dict]) -> list[tuple[str, float]]:
    """Tiingo 日线的 splitFactor ≠ 1 的日子即拆股（合股为小于 1 的比例）。"""
    return [(r["date"][:10], float(r["splitFactor"])) for r in rows
            if r.get("splitFactor") not in (None, 1, 1.0)]


def close_frame(rows: list[dict], field: str) -> pd.DataFrame:
    """日线 → us-dcf 要的形状：列 Close、日期索引、升序。"""
    df = pd.DataFrame({"Close": [float(r[field]) for r in rows if r.get(field) is not None]},
                      index=pd.to_datetime([r["date"][:10] for r in rows if r.get(field) is not None]))
    return df.sort_index()


def reconcile(frames: dict, ledger: FactLedger, as_of: date, fye: str) -> list[str]:
    """报表（百万美元）与账本（美元）的核心科目逐年对账。账本没有的不比（缺了就是缺了）。"""
    out = []
    for table, col, key in CORE:
        df = frames.get(table)
        if df is None or col not in df.columns:
            continue
        for end in frames.get("ends") or list(df.index):
            v = df.loc[end, col] if end in df.index else None
            if v is None or pd.isna(v):
                continue
            period = fiscal_label(None, date.fromisoformat(str(end)[:10]), fye)
            try:
                led = ledger.get(key, period, as_of=as_of).value / MN
            except (KeyError, LookAheadError):
                continue
            ours = Decimal(str(float(v)))
            if led == 0:
                continue
            rel = abs(ours - led) / abs(led)
            if rel > RECONCILE_TOL:
                out.append(f"估值层与分析层 {col}@{period} 不一致：报表 {ours:.1f} vs 账本 {led:.1f} 百万美元"
                           f"（相对差 {rel:.2%}）—— 两层选取的 XBRL 标签可能不同")
    return out


def market_inputs(ticker: str, as_of: date, store, ledger: FactLedger) -> dict:
    """us-dcf 要的行情：现价（账本）、个股日线与拆股（Tiingo）、标普 500（FRED SP500）。"""
    import requests

    from ir_agent.sources import fred
    from ir_agent.sources.edgar import normalize_ticker
    from ir_agent.sources.us_quotes import TIINGO_URL, QuoteUnavailable, _env

    start = (as_of - timedelta(days=366 * 6)).isoformat()
    t = normalize_ticker(ticker).lower()
    r = requests.get(TIINGO_URL.format(ticker=t), params={"startDate": start, "endDate": as_of.isoformat()},
                     headers={"Authorization": f"Token {_env('TIINGO_API_KEY')}"}, timeout=30)
    if r.status_code != 200:
        raise QuoteUnavailable(f"Tiingo 日线 HTTP {r.status_code}")
    rows = r.json()
    store.save(source="tiingo_daily_history", payload=rows, url=TIINGO_URL.format(ticker=t),
               fetched_at=datetime.now(), params={"startDate": start, "endDate": as_of.isoformat()})
    sp = fred.history("SP500", as_of - timedelta(days=366 * 6), as_of, store)
    mkt = pd.DataFrame({"Close": [float(v) for _, v in sp]}, index=pd.to_datetime([d for d, _ in sp]))
    price = next((f for (k, _p), fs in ledger._facts.items() if k == "last_price"      # noqa: SLF001
                  for f in fs if f.as_of <= as_of), None)
    return dict(quote=dict(source="ir-agent 账本（FMP → Tiingo）",
                           price=float(price.value) if price is not None else float(rows[-1]["close"])),
                px=close_frame(rows, "adjClose"), mkt=mkt.sort_index(),
                splits=splits_from_tiingo(rows))
