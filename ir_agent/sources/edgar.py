"""SEC EDGAR 客户端 —— 美股三表与公司元数据的主源。

三个接口:
  1. `company_tickers_exchange.json`：代码 → CIK、公司名、交易所；
  2. `submissions/CIK##########.json`：SIC 行业代码、财年末（MMDD）、申报清单；
  3. `api/xbrl/companyfacts/CIK##########.json`：全部 XBRL 事实（含每条的申报日 `filed`）。

SEC 的两条硬规矩:
  - 请求头必须写明身份与联系方式。身份只从环境变量 `SEC_USER_AGENT` 读，代码里不留邮箱；
    缺失时在发出任何请求之前报错。
  - 每秒不超过 10 次。这里按 9 次限流，串行请求。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from ir_agent.sources.retry import with_retry

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
TIMEOUT = 30
_MIN_INTERVAL = 1 / 9
_last_request = 0.0


class SecIdentityMissing(Exception):
    """未设置 SEC_USER_AGENT。"""


class UnknownTicker(Exception):
    """SEC 代码表里没有这个代码。"""


@dataclass(frozen=True)
class TickerInfo:
    cik: str            # 10 位补零
    name: str
    exchange: str


def sec_headers() -> dict[str, str]:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise SecIdentityMissing(
            "未设置 SEC_USER_AGENT。SEC 要求请求头写明身份与联系邮箱，例如：\n"
            '  export SEC_USER_AGENT="Your Name your@email"')
    return {"User-Agent": ua}


def pad_cik(cik: str | int) -> str:
    return str(int(cik)).zfill(10)


def normalize_ticker(ticker: str) -> str:
    """SEC 代码表用连字符写类别股（BRK-B）；行情源常写 BRK.B。统一成 SEC 写法。"""
    return ticker.strip().upper().replace(".", "-")


def _throttle() -> None:
    global _last_request
    wait = _last_request + _MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _get_json(url: str, source: str, store) -> tuple[dict, str]:
    headers = sec_headers()                     # 缺身份在这里就报错，不发请求

    def once():
        _throttle()
        r = requests.get(url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    payload = with_retry(once)
    fetched_at = datetime.now()
    sid = store.save(source=source, payload=payload, url=url, fetched_at=fetched_at)
    return payload, sid


def get_text(url: str, params: dict, source: str, store) -> tuple[str, str]:
    """取 HTML 页面（如按 SIC 列公司），同样的身份与限流规则，原文落快照。"""
    headers = sec_headers()

    def once():
        _throttle()
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text

    text = with_retry(once)
    sid = store.save(source=source, payload=text, url=url, fetched_at=datetime.now(),
                     params=params)
    return text, sid


def load_tickers(store) -> tuple[dict[str, TickerInfo], str]:
    """全部代码 → TickerInfo。同业名单（只留有代码、在交易所上市的公司）也用这张表。"""
    doc, sid = _get_json(TICKERS_URL, "sec_tickers", store)
    i = {f: k for k, f in enumerate(doc["fields"])}
    table = {}
    for row in doc["data"]:
        t = normalize_ticker(row[i["ticker"]])
        table.setdefault(t, TickerInfo(cik=pad_cik(row[i["cik"]]), name=row[i["name"]],
                                       exchange=row[i["exchange"]] or ""))
    return table, sid


def resolve_ticker(ticker: str, store) -> TickerInfo:
    table, _ = load_tickers(store)
    t = normalize_ticker(ticker)
    if t not in table:
        raise UnknownTicker(f"SEC 代码表里没有 {ticker}（已按 {t} 查找）。")
    return table[t]


def fetch_submissions(cik: str | int, store) -> tuple[dict, str]:
    c = pad_cik(cik)
    return _get_json(SUBMISSIONS_URL.format(cik=c), "sec_submissions", store)


def fetch_companyfacts(cik: str | int, store) -> tuple[dict, str]:
    c = pad_cik(cik)
    return _get_json(COMPANYFACTS_URL.format(cik=c), "sec_companyfacts", store)
