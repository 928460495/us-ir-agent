from datetime import date
from pathlib import Path

import pytest

from ir_agent.sources import announce, edgar, edgar_filings
from ir_agent.sources.external import Corpus, Tier
from ir_agent.sources.snapshot import SnapshotStore

FIX = Path(__file__).parent / "fixtures" / "edgar"
AS_OF = date(2026, 8, 15)


def _sub(rows):
    keys = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument", "items")
    d = {"cik": "320193", "name": "Apple Inc.",
         "filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(keys)}}}
    return d


EARN = ("8-K", "2026-07-30", "2026-07-30", "0000320193-26-000018", "aapl-20260730.htm", "2.02,9.01")
VOTE = ("8-K", "2026-02-24", "2026-02-24", "0001140361-26-006577", "ef_8k.htm", "5.07,9.01")
DEAL = ("8-K", "2026-03-10", "2026-03-10", "0000320193-26-000009", "aapl-20260310.htm", "1.01")
LATE = ("8-K", "2026-10-30", "2026-10-30", "0000320193-26-000030", "aapl-20261030.htm", "2.02,9.01")
OLD = ("8-K", "2025-01-30", "2025-01-30", "0000320193-25-000003", "aapl-20250130.htm", "2.02,9.01")


@pytest.fixture
def fake(monkeypatch):
    pages = {"index": "8k_index_ex991.htm"}

    def get_text(url, params, source, store):
        if url.endswith("-index.htm"):
            return (FIX / pages["index"]).read_text(encoding="utf-8"), "sid_idx"
        if "ex991" in url:
            return (FIX / "8k_ex991.htm").read_text(encoding="utf-8"), "sid_ex"
        return "<html><body><p>Item 1.01 Entry into a Material Definitive Agreement. The Company entered into an agreement with a supplier.</p></body></html>", "sid_doc"
    monkeypatch.setattr(edgar, "get_text", get_text)
    return pages


def _gather(rows, fake_pages=None, as_of=AS_OF):
    corpus = Corpus(as_of=as_of)
    res = edgar_filings.gather_us(sub=_sub(rows), as_of=as_of, corpus=corpus,
                                  store=SnapshotStore("/tmp/unused-store"), company="Apple Inc.")
    return res, corpus


def test_业绩8K取EX991正文(fake, tmp_path):
    res, corpus = _gather([EARN])
    d = res.docs[0]
    assert d.tier is Tier.COMPANY and d.rank == 1 and d.publisher == "Apple Inc."
    assert "Apple reports third quarter results" in d.text
    assert d.published == date(2026, 7, 30) and "业绩发布" in d.title
    assert d.url.endswith("a8-kex991q3202606272026.htm")
    assert corpus.docs == res.docs


def test_没有EX991时退到EX99(fake):
    fake["index"] = "8k_index_ex99.htm"
    res, _ = _gather([EARN])
    assert len(res.docs) == 1 and "Apple reports" in res.docs[0].text


def test_两者都没有时计入失败并写明accession(fake):
    fake["index"] = "8k_index_noexhibit.htm"
    res, _ = _gather([EARN])
    assert res.docs == [] and res.fetch_failed == 1
    assert "0000320193-26-000018" in res.fetch_failed_reasons[0]


def test_实质事项取主文档_程序性事项排除(fake):
    res, _ = _gather([DEAL, VOTE])
    assert [d.title for d in res.docs] == ["8-K 重大协议（2026-03-10）"]
    assert res.docs[0].rank == 1 and res.rejected == 1 and res.candidates == 2


def test_晚于as_of与早于窗口的不入库(fake):
    res, _ = _gather([LATE, OLD, EARN])
    assert [d.published for d in res.docs] == [date(2026, 7, 30)]
    assert res.candidates == 1


def test_落盘读回往返(fake, tmp_path):
    res, _ = _gather([EARN, DEAL])
    announce.save(res, tmp_path / "a.json")
    back = announce.load(tmp_path / "a.json", Corpus(as_of=AS_OF))
    assert [(d.doc_id, d.text) for d in back.docs] == [(d.doc_id, d.text) for d in res.docs]


def test_美股公告走8K且不碰巨潮(fake, monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from ir_agent import full
    monkeypatch.setattr(announce, "gather", lambda **k: (_ for _ in ()).throw(AssertionError("巨潮")))
    store = SnapshotStore(tmp_path / "snap")
    from datetime import datetime
    sid = store.save(source="sec_submissions", payload=_sub([EARN, VOTE]), url="u", fetched_at=datetime.now())
    from ir_agent.sources import transcripts
    from ir_agent.sources.external import Document
    call = Document(doc_id="defeatbeta_AAPL_2026Q3", url="u", title="FY2026 Q3 业绩电话会纪要",
                    publisher=transcripts.PUBLISHER, tier=Tier.COMPANY, published=date(2026, 7, 30),
                    retrieved=AS_OF, text="Tim Cook: hello")
    monkeypatch.setattr(transcripts, "fetch_transcripts", lambda t, as_of, store: ([call], ""))
    r = SimpleNamespace(code="AAPL", corpus=None, store=store, sec_meta={"submissions_sid": sid})
    md = {}
    notes = full._load_announce("AAPL", r, AS_OF, tmp_path, md)
    assert [d.doc_id for d in r.corpus.docs] == ["sec8k_0000320193-26-000018", "defeatbeta_AAPL_2026Q3"]
    back = announce.load(tmp_path / announce.ANNOUNCE_JSON, Corpus(as_of=AS_OF))
    assert len(back.docs) == 2 and notes == []


def test_纪要取不到时记一条说明不中断(fake, monkeypatch, tmp_path):
    from datetime import datetime
    from types import SimpleNamespace
    from ir_agent import full
    from ir_agent.sources import transcripts
    monkeypatch.setattr(transcripts, "fetch_transcripts",
                        lambda t, as_of, store: ([], "电话会纪要（defeatbeta 不可达：OSError）"))
    store = SnapshotStore(tmp_path / "snap")
    sid = store.save(source="sec_submissions", payload=_sub([EARN]), url="u", fetched_at=datetime.now())
    r = SimpleNamespace(code="AAPL", corpus=None, store=store, sec_meta={"submissions_sid": sid})
    notes = full._load_announce("AAPL", r, AS_OF, tmp_path, {})
    assert len(r.corpus.docs) == 1 and any("defeatbeta 不可达" in n for n in notes)
