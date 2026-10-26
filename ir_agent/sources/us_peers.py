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
from ir_agent.sources import edgar, xbrl_instance
from ir_agent.sources.edgar_facts import MONEY_UNIT, parse_companyfacts, period_ends, slim
from ir_agent.sources.us_quotes import latest_shares

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


_FILING_COLS = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")


def _slim_submissions(doc: dict) -> dict:
    """同业要财年末、公司名，以及定位最新一份 10-K / 10-Q 的申报清单列（多类股公司要从
    XBRL 实例补在外股数）。其余列与历史分页（filings.files）不落快照。"""
    out = {k: v for k, v in doc.items() if k != "filings"}
    rec = (doc.get("filings") or {}).get("recent") or {}
    out["filings"] = {"recent": {c: rec.get(c, []) for c in _FILING_COLS}}
    return out


def _one(ticker: str, name: str, cik: str, store, as_of: date) -> PeerBook | str:
    # 缓存源名带「_slim」：裁剪规则变过（早先连申报清单一起裁掉），旧缓存不能拿来补多类股股数
    sub, sub_sid = edgar.fetch_json_cached(edgar.SUBMISSIONS_URL.format(cik=cik), "sec_submissions_slim",
                                           store, as_of, transform=_slim_submissions,
                                           params={"slim": "申报清单只留定位列"})
    fye = sub.get("fiscalYearEnd") or ""
    if len(fye) != 4:
        return f"submissions 没有财年末（fiscalYearEnd={fye!r}）"
    doc, cf_sid = edgar.fetch_json_cached(edgar.COMPANYFACTS_URL.format(cik=cik), "sec_companyfacts",
                                          store, as_of, transform=slim, params={"slim": SLIM_NOTE})
    if not doc.get("facts", {}).get("us-gaap"):
        return "没有 us-gaap 事实（多为只报 IFRS 的外国发行人）"
    facts = parse_companyfacts(doc, fye, cf_sid, notes=[])
    if not any(f.unit == MONEY_UNIT for f in facts):
        # 实测 AAPL 同业 10 家落在这里，多为以色列等外国发行人：报 us-gaap、但申报表是 20-F / 40-F，
        # 不在 10-K / 10-Q 范围内（年报一年一份、无季报，时点与 TTM 口径都对不上本司）
        return "没有 10-K / 10-Q 申报的美元数据（多为提交 20-F / 40-F 的外国发行人）"
    if latest_shares(facts, as_of)[0] is None:
        # 多类股公司（GOOGL、BRK）按类别申报股数，companyfacts 不收 —— 从最新一份申报的实例里加总
        facts += [f for f in xbrl_instance.load_dimensional(cik, sub, fye, store, as_of, [], latest_only=True)
                  if f.key == "shares_outstanding"]
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
