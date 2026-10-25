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
