"""FMP（Financial Modeling Prep）免费档 —— 美股三表的**交叉核对源**，不用于取值。

免费档的限制决定了它只能当次源（2026-10-25 实测）:
  - 只认 `/stable/` 端点；`limit` 最大 5；
  - **标的范围受限**：AAPL、COST 可用，CAT 返回 402 —— 402 必须记为「交叉核对不可用」，
    绝不能因为「没发现分歧」就报一致；
  - 季度数据是**单季**，账本是累计口径 —— 流量科目只比 FY 与 Q1（Q1 单季即累计），
    存量科目各期都比。

key 只从环境变量 `FMP_API_KEY` 读，只放在请求参数里：快照与错误信息都不带 key。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal

import requests

from ir_agent.ledger import Fact, Method
from ir_agent.sources.us_periods import fiscal_label

BASE = "https://financialmodelingprep.com/stable"
TIMEOUT = 20
LIMIT = 5                                   # 免费档上限，超过返回 402

# (端点, 期间类型) → {FMP 字段: 账本键}
_FLOWS = {"revenue": "revenue", "operatingIncome": "operating_profit",
          "netIncome": "net_profit_attr_parent"}
_STOCKS = {"totalAssets": "total_assets", "totalStockholdersEquity": "equity_attr_parent"}
_CALLS = (("income-statement", "annual", _FLOWS), ("income-statement", "quarter", _FLOWS),
          ("balance-sheet-statement", "annual", _STOCKS),
          ("balance-sheet-statement", "quarter", _STOCKS))


class FmpUnavailable(Exception):
    """FMP 取不到：缺 key、标的不在免费档范围（402）、key 无效（401/403）等。"""


def _key() -> str:
    k = os.environ.get("FMP_API_KEY", "").strip()
    if not k:
        raise FmpUnavailable("FMP 不可用：未设置 FMP_API_KEY")
    return k


def _get(endpoint: str, ticker: str, period: str, key: str, store) -> tuple[list, str]:
    url = f"{BASE}/{endpoint}"
    public = {"symbol": ticker, "period": period, "limit": LIMIT}
    try:
        r = requests.get(url, params={**public, "apikey": key}, timeout=TIMEOUT)
    except requests.RequestException as e:          # 异常文本里可能带完整 URL（含 key）
        raise FmpUnavailable(f"FMP 不可用：{endpoint} 网络错误 {e.__class__.__name__}") from None
    if r.status_code != 200:
        reason = {402: "标的或参数不在免费档范围", 401: "key 无效", 403: "端点不可用"}.get(
            r.status_code, "请求失败")
        raise FmpUnavailable(f"FMP 不可用：{endpoint} HTTP {r.status_code}（{reason}）")
    payload = r.json()
    if not isinstance(payload, list):
        raise FmpUnavailable(f"FMP 不可用：{endpoint} 返回非列表（{str(payload)[:80]}）")
    sid = store.save(source=f"fmp_{endpoint.replace('-', '_')}_{period}", payload=payload,
                     url=url, fetched_at=datetime.now(), params=public)
    return payload, sid


def _parse(rows: list, fields: dict[str, str], quarterly: bool, flows: bool,
           fye_mmdd: str, source_id: str) -> list[Fact]:
    out = []
    for row in rows:
        filed, end = row.get("filingDate"), row.get("date")
        if not filed or not end:
            continue
        if row.get("reportedCurrency", "USD") != "USD":
            continue                                # 非美元报告（如 TSM 新台币）不能标成美元
        if quarterly and flows and row.get("period") != "Q1":
            continue                                # 单季流量只有 Q1 与累计口径相同
        period = fiscal_label(None, date.fromisoformat(end[:10]), fye_mmdd)
        if period is None:
            continue
        for fld, key in fields.items():
            if row.get(fld) is None:
                continue
            out.append(Fact(key=key, value=Decimal(str(row[fld])), unit="美元", currency="USD",
                            period=period, as_of=date.fromisoformat(filed[:10]),
                            source_id=source_id, method=Method.REPORTED))
    return out


def fetch_statements_fmp(ticker: str, store, fye_mmdd: str,
                         notes: list[str] | None = None) -> list[Fact]:
    """只取营收、营业利润、归母净利润、总资产、归母权益，用于与 EDGAR 比对。

    四个请求全部失败才抛 FmpUnavailable；部分失败（如免费档不给季度）记进 notes。
    """
    key = _key()
    facts, errors = [], []
    for endpoint, period, fields in _CALLS:
        try:
            rows, sid = _get(endpoint, ticker, period, key, store)
        except FmpUnavailable as e:
            errors.append(str(e))
            continue
        facts += _parse(rows, fields, period == "quarter", fields is _FLOWS, fye_mmdd, sid)
    if len(errors) == len(_CALLS):
        raise FmpUnavailable(errors[0])
    if notes is not None:
        notes.extend(errors)
    return facts


CONSENSUS_MAX_LAG_DAYS = 3      # FMP 只给当前快照：分析时点距今超过这么多天就拿不到「当时」的预期


def _snapshot_rows(store, source: str, ticker: str, as_of: date) -> list | None:
    """分析时点附近（±三天）已落的快照 —— 后续阶段读回它，不重新请求。

    时点守卫看的是**快照抓取日**与 as_of 的距离，不是「今天」：门①签名后隔几天再跑
    第二次调用，as_of 不变，第一阶段写进提示词的 [[consensus.eps@…]] 必须还在账本里
    （审查发现：按「今天」判时，隔三天以上就会挂引用核验）。
    """
    import json
    from datetime import datetime, timedelta
    from pathlib import Path

    best = None
    for p in Path(store.root).glob(f"{source}_*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            got = datetime.fromisoformat(d["fetched_at"]).date()
        except Exception:                                    # noqa: BLE001
            continue
        if (d.get("params") or {}).get("symbol") != ticker:
            continue
        if abs((got - as_of).days) > CONSENSUS_MAX_LAG_DAYS:
            continue
        if best is None or got > best[0]:
            best = (got, d.get("payload"))
    return best[1] if best and isinstance(best[1], list) else None


def fetch_consensus(ticker: str, fye_mmdd: str, as_of: date, store,
                    today: date | None = None):
    """FMP 分析师预期 → reports.Consensus（统计量为均值）。取不到返回 None。

    时点守卫：FMP 只给**当前**的预期。回测时点（as_of 距今超过三天）拿到的是未来才形成的
    预期，宁可没有。单人覆盖的年份不收 —— 与 A 股「单家券商不构成一致预期」同一规则。
    """
    from datetime import timedelta

    from ir_agent.sources.reports import Consensus, Estimate

    rows = _snapshot_rows(store, "fmp_analyst_estimates_annual", ticker, as_of)
    if rows is None:
        if as_of < (today or date.today()) - timedelta(days=CONSENSUS_MAX_LAG_DAYS):
            return None
        try:
            rows, _sid = _get("analyst-estimates", ticker, "annual", _key(), store)
        except FmpUnavailable:
            return None
    eps: dict[int, Estimate] = {}
    for row in rows:
        n = int(row.get("numAnalystsEps") or 0)
        if n < 2 or row.get("epsAvg") is None or not row.get("date"):
            continue
        label = fiscal_label(None, date.fromisoformat(row["date"][:10]), fye_mmdd)
        if label is None:
            continue
        y = int(label[:4])
        eps[y] = Estimate(fiscal_year=y, n=n, median=Decimal(str(row["epsAvg"])),
                          low=Decimal(str(row.get("epsLow", row["epsAvg"]))),
                          high=Decimal(str(row.get("epsHigh", row["epsAvg"]))))
    if not eps:
        return None
    return Consensus(as_of=as_of, window_days=0, eps=eps, n_reports=0, stat="均值",
                     provider="FMP 分析师预期，统计量为均值；分析时点的快照")

