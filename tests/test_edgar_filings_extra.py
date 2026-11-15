"""8-K 近一手来源扩展（用户 2026-11-15 决定）：5.02 人事变动入库；业绩 8-K 的 EX-99.2 起每个附件各成
一篇（投资者演示、补充数据）。不联网。"""

from datetime import date
from pathlib import Path

import pytest

from ir_agent.sources import edgar, edgar_filings
from ir_agent.sources.external import Corpus, Tier
from ir_agent.sources.snapshot import SnapshotStore

FIX = Path(__file__).parent / "fixtures" / "edgar"
AS_OF = date(2026, 8, 15)
ACC = "0000320193-26-000018"
EARN = ("8-K", "2026-07-30", "2026-07-30", ACC, "aapl-20260730.htm", "2.02,9.01")
EXEC = ("8-K", "2026-05-01", "2026-05-01", "0000320193-26-000011", "aapl-20260501.htm", "5.02")

_EX992_ROW = """         <tr>
            <td scope="row">3</td>
            <td scope="row">EX-99.2</td>
            <td scope="row"><a href="/Archives/edgar/data/320193/000032019326000018/a8-kex992.htm">a8-kex992.htm</a></td>
            <td scope="row">EX-99.2</td>
            <td scope="row">5000</td>
         </tr>
"""


def _sub(rows):
    keys = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument", "items")
    return {"cik": "320193", "name": "Apple Inc.",
            "filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(keys)}}}


@pytest.fixture
def fake(monkeypatch):
    state = {"fail_992": False}
    idx = (FIX / "8k_index_ex991.htm").read_text(encoding="utf-8")
    idx = idx.replace("         <tr>\n            <td scope=\"row\">7</td>", _EX992_ROW + "         <tr>\n            <td scope=\"row\">7</td>")

    def get_text(url, params, source, store):
        if url.endswith("-index.htm"):
            return idx, "sid_idx"
        if "ex991" in url:
            return (FIX / "8k_ex991.htm").read_text(encoding="utf-8"), "sid_ex"
        if "ex992" in url:
            if state["fail_992"]:
                raise ConnectionError("boom")
            return "<html><body><p>Supplemental data: services revenue by region.</p></body></html>", "sid_992"
        return "<html><body><p>Item 5.02 Departure of Directors or Certain Officers. The CFO will retire.</p></body></html>", "sid_doc"
    monkeypatch.setattr(edgar, "get_text", get_text)
    return state


def _gather(rows):
    corpus = Corpus(as_of=AS_OF)
    res = edgar_filings.gather_us(sub=_sub(rows), as_of=AS_OF, corpus=corpus,
                                  store=SnapshotStore("/tmp/unused-store"), company="Apple Inc.")
    return res


def test_人事变动8K入库(fake):
    res = _gather([EXEC])
    assert len(res.docs) == 1 and "高管或董事变动" in res.docs[0].title
    assert res.docs[0].tier is Tier.COMPANY and "CFO will retire" in res.docs[0].text


def test_业绩8K的其他附件各成一篇(fake):
    res = _gather([EARN])
    assert [d.doc_id for d in res.docs] == [f"sec8k_{ACC}", f"sec8k_{ACC}_EX-99.2"]
    extra = res.docs[1]
    assert extra.title == "8-K 业绩附件 EX-99.2（2026-07-30）" and extra.rank == 2
    assert extra.tier is Tier.COMPANY and "services revenue by region" in extra.text
    assert res.docs[0].rank == 1


def test_附件取失败只记说明不影响主新闻稿(fake):
    fake["fail_992"] = True
    res = _gather([EARN])
    assert [d.doc_id for d in res.docs] == [f"sec8k_{ACC}"]
    assert any("EX-99.2" in x and ACC in x for x in res.fetch_failed_reasons)
