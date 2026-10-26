"""美股同业财报：逐家取 companyfacts，与本司走同一条解析链（`parse_companyfacts`）。

**不用 XBRL frames**（设计原写 frames，2026-10-26 实测后改）：frames 每条只有
`accn/cik/entityName/loc/start/end/val`，没有申报日 `filed`，时点守卫做不了；只给最后申报的值
（含重述）；没有年初至今的帧，非年末截止的 TTM 算不出。代价是一个行业几十到上百次下载，
由 `edgar.fetch_json_cached` 按分析时点缓存、快照裁剪到映射表科目。

一家失败不拖垮整批：只报 IFRS 的外国发行人、HTTP 失败、缺财年末，各留一条说明后剔除。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date

from ir_agent.ledger import Fact
from ir_agent.sources import edgar
from ir_agent.sources.edgar_facts import MONEY_UNIT, parse_companyfacts, period_ends, slim

SLIM_NOTE = "仅保留映射表科目与 dei"
PROGRESS_EVERY = 25


@dataclass
class PeerBook:
    ticker: str
    name: str
    cik: str
    fye: str
    facts: list[Fact]
    ends: dict[str, date]                     # 期间标签 → 期末日
    sids: dict[str, str] = field(default_factory=dict)


def _slim_submissions(doc: dict) -> dict:
    """同业只要财年末与公司名；申报清单（filings）占了 submissions 的大头，不落快照。"""
    return {k: v for k, v in doc.items() if k != "filings"}


def _one(ticker: str, name: str, cik: str, store, as_of: date) -> PeerBook | str:
    sub, sub_sid = edgar.fetch_json_cached(edgar.SUBMISSIONS_URL.format(cik=cik), "sec_submissions",
                                           store, as_of, transform=_slim_submissions,
                                           params={"slim": "去掉申报清单"})
    fye = sub.get("fiscalYearEnd") or ""
    if len(fye) != 4:
        return f"submissions 没有财年末（fiscalYearEnd={fye!r}）"
    doc, cf_sid = edgar.fetch_json_cached(edgar.COMPANYFACTS_URL.format(cik=cik), "sec_companyfacts",
                                          store, as_of, transform=slim, params={"slim": SLIM_NOTE})
    if not doc.get("facts", {}).get("us-gaap"):
        return "没有 us-gaap 事实（多为只报 IFRS 的外国发行人）"
    facts = parse_companyfacts(doc, fye, cf_sid, notes=[])
    if not any(f.unit == MONEY_UNIT for f in facts):
        return "us-gaap 事实里没有以美元计的定期报告数据"
    return PeerBook(ticker=ticker, name=name, cik=cik, fye=fye, facts=facts,
                    ends=period_ends(doc, fye), sids={"submissions": sub_sid, "companyfacts": cf_sid})


def load_peer_books(members, store, as_of: date, notes: list[str]) -> dict[str, PeerBook]:
    """members: [Member(code=ticker, name)]。返回 {ticker: PeerBook}；失败逐家写进 notes。"""
    tickers, _ = edgar.load_tickers(store)
    out: dict[str, PeerBook] = {}
    for i, m in enumerate(members, 1):
        if i % PROGRESS_EVERY == 0:
            print(f"  同业财报 {i}/{len(members)}…", file=sys.stderr)
        info = tickers.get(edgar.normalize_ticker(m.code))
        if info is None:
            notes.append(f"{m.code} {m.name}：SEC 代码表里没有，剔除")
            continue
        try:
            got = _one(m.code, m.name, info.cik, store, as_of)
        except edgar.SecIdentityMissing:
            raise
        except Exception as e:                              # noqa: BLE001
            notes.append(f"{m.code} {m.name}：财报取数失败（{e.__class__.__name__}），剔除")
            continue
        if isinstance(got, str):
            notes.append(f"{m.code} {m.name}：{got}，剔除")
        else:
            out[m.code] = got
    return out
