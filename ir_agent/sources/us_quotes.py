"""美股行情与市值。

主源 FMP `/stable/quote`；FMP 不可用（缺 key、标的不在免费档范围）时退到 Tiingo 日终收盘价。
两源都不可用就报错 —— 不拿缓存里的旧价顶替，谎称有价格比没有价格更危险。
不用 Stooq：其下载接口已改为浏览器 JS 验证页（2026-10-25 实测），程序取不到，也不应绕过。

**市值 = 收盘价 × EDGAR 在外股数**（时点内可见的最新一期）。FMP 的 marketCap 只作核对，
相差超过 2% 记为不一致 —— 股数口径（是否含多类股、回购是否已注销）是常见病因。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import requests

from ir_agent.ledger import Fact, Method
from ir_agent.periods_spot import spot_period
from ir_agent.sources.edgar import normalize_ticker

FMP_QUOTE_URL = "https://financialmodelingprep.com/stable/quote"
TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
TIMEOUT = 20
CAP_TOLERANCE = Decimal("0.02")


class QuoteUnavailable(Exception):
    """行情取不到。"""


@dataclass(frozen=True)
class Quote:
    price: Decimal
    as_of: date
    source: str
    source_id: str
    market_cap: Decimal | None = None       # 仅 FMP 提供，用于核对


@dataclass(frozen=True)
class CapCheck:
    status: str                              # "一致" | "不一致" | "不可用"
    rel_diff: Decimal | None = None


def _env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise QuoteUnavailable(f"行情不可用：未设置 {name}")
    return v


def _http(url: str, what: str, **kw):
    try:
        r = requests.get(url, timeout=TIMEOUT, **kw)
    except requests.RequestException as e:   # 异常文本可能带 key，不透传
        raise QuoteUnavailable(f"{what} 网络错误 {e.__class__.__name__}") from None
    if r.status_code != 200:
        raise QuoteUnavailable(f"{what} HTTP {r.status_code}")
    return r.json()


def _fmp_quote(ticker: str, store) -> Quote:
    t = normalize_ticker(ticker)
    body = _http(FMP_QUOTE_URL, "FMP 报价", params={"symbol": t, "apikey": _env("FMP_API_KEY")})
    if not isinstance(body, list) or not body or body[0].get("price") is None:
        raise QuoteUnavailable(f"FMP 报价为空（{t}）")
    q = body[0]
    sid = store.save(source="fmp_quote", payload=body, url=FMP_QUOTE_URL,
                     fetched_at=datetime.now(), params={"symbol": t})
    as_of = datetime.fromtimestamp(int(q["timestamp"]), tz=timezone.utc).date()
    cap = q.get("marketCap")
    return Quote(price=Decimal(str(q["price"])), as_of=as_of, source="fmp", source_id=sid,
                 market_cap=Decimal(str(cap)) if cap is not None else None)


def _tiingo_close(ticker: str, store) -> Quote:
    t = normalize_ticker(ticker).lower()
    url = TIINGO_URL.format(ticker=t)
    body = _http(url, "Tiingo 日线",
                 headers={"Authorization": f"Token {_env('TIINGO_API_KEY')}",
                          "Content-Type": "application/json"})
    if not isinstance(body, list) or not body or body[-1].get("close") is None:
        raise QuoteUnavailable(f"Tiingo 日线为空（{t}）")
    last = body[-1]
    sid = store.save(source="tiingo_daily", payload=body, url=url, fetched_at=datetime.now())
    return Quote(price=Decimal(str(last["close"])), as_of=date.fromisoformat(last["date"][:10]),
                 source="tiingo", source_id=sid)


def build_quote_facts(q: Quote, shares: Fact | None) -> list[Fact]:
    """行情是即期量，period 用报价日。没有股数就只出股价 —— 不编市值。"""
    sp = spot_period(q.as_of)
    out = [Fact(key="last_price", value=q.price, unit="美元", currency="USD", period=sp,
                as_of=q.as_of, source_id=q.source_id, method=Method.REPORTED)]
    if shares is not None:
        out.append(Fact(key="market_cap", value=q.price * shares.value, unit="美元",
                        currency="USD", period=sp, as_of=max(q.as_of, shares.as_of),
                        source_id=q.source_id, method=Method.COMPUTED,
                        derived_from=(f"last_price@{sp}", f"shares_outstanding@{shares.period}")))
    return out


def compare_cap(ours: Decimal, fmp_cap: Decimal | None) -> CapCheck:
    if fmp_cap is None or ours == 0:
        return CapCheck("不可用")
    rel = abs(fmp_cap - ours) / abs(ours)
    return CapCheck("不一致" if rel > CAP_TOLERANCE else "一致", rel)


def fetch_quote_us(ticker: str, store, shares: Fact | None = None,
                   notes: list[str] | None = None) -> list[Fact]:
    notes = notes if notes is not None else []
    errors = []
    for fetch in (_fmp_quote, _tiingo_close):
        try:
            q = fetch(ticker, store)
            break
        except QuoteUnavailable as e:
            errors.append(str(e))
    else:
        raise QuoteUnavailable(f"{ticker} 行情两源均不可用：" + "；".join(errors))
    notes.extend(f"行情降级：{e}" for e in errors)

    facts = build_quote_facts(q, shares)
    cap = next((f.value for f in facts if f.key == "market_cap"), None)
    if cap is not None:
        chk = compare_cap(cap, q.market_cap)
        if chk.status == "不一致":
            notes.append(f"市值核对不一致：股价×EDGAR 股数 {cap} vs FMP {q.market_cap}"
                         f"（相对差 {chk.rel_diff:.2%}）")
        elif chk.status == "不可用":
            notes.append("市值核对不可用：行情源未提供市值（Tiingo 兜底时没有 FMP 市值）")
    return facts
