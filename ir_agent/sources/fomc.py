"""FOMC 政策声明 → 语料库（OFFICIAL）。美股宏观定性的官方文字依据。

A 股的宏观定性读券商宏观报告；美股免费渠道没有卖方研报，但有更好的：美联储自己的
政策声明 —— 政策取向不必转述，原文就在这里。

**来源用会议日历页，不用 RSS**：RSS 只有最近约半年（2026-10-25 实测最早到 2026-04），
日历页列着近五年每次会议的声明链接，回测时点也取得到。声明链接形如
`/newsevents/pressreleases/monetaryYYYYMMDDa.htm`，日期即发布日（会议当天下午发布）；
`…a1.htm` 是实施说明，不收。
"""

from __future__ import annotations

import re
from datetime import date, datetime

import requests

from ir_agent.sources.external import Document, Tier
from ir_agent.sources.tenk import html_text

BASE = "https://www.federalreserve.gov"
CALENDAR = BASE + "/monetarypolicy/fomccalendars.htm"
_STATEMENT = re.compile(r'href="(/newsevents/pressreleases/monetary(\d{8})a\.htm)"')
TIMEOUT = 30


def _get(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _article(html: str) -> str:
    i = html.find('id="article"')
    if i < 0:
        return html_text(html)
    j = html.find('id="lastUpdate"', i)
    return html_text(html[html.rfind("<", 0, i):j if j > 0 else len(html)])


def fetch_statements(as_of: date, store, n: int = 2) -> tuple[list[Document], str]:
    """as_of 之前最近 n 份声明。取不到返回 ([], 说明)，不抛 —— 宏观定性会如实写「缺文字依据」。"""
    try:
        cal = _get(CALENDAR)
    except Exception as e:                                   # noqa: BLE001
        return [], f"FOMC 声明（会议日历取不到：{e.__class__.__name__}）"
    links = {(date(int(d[:4]), int(d[4:6]), int(d[6:])), path)
             for path, d in _STATEMENT.findall(cal)}
    visible = sorted((x for x in links if x[0] <= as_of), reverse=True)[:n]
    if not visible:
        return [], f"FOMC 声明（会议日历覆盖不到 {as_of.isoformat()} 之前）"
    docs = []
    for day, path in visible:
        try:
            html = _get(BASE + path)
        except Exception as e:                               # noqa: BLE001
            return docs, f"FOMC 声明（{path} 取不到：{e.__class__.__name__}）"
        store.save(source="fomc_statement", payload=html, url=BASE + path,
                   fetched_at=datetime.now())
        docs.append(Document(doc_id=f"fomc_{day:%Y%m%d}", url=BASE + path,
                             publisher="美联储（FOMC）", tier=Tier.OFFICIAL, title="FOMC 政策声明",
                             published=day, retrieved=as_of, text=_article(html)))
    return docs, ""
