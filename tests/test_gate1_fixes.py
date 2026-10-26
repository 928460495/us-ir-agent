"""门① 来源清单与 ① 产出的修正（AAPL 第三次重跑 ① 后排查，2026-10-26）。不联网。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.sources.external import Corpus, Document, Tier
from ir_agent.sources.snapshot import SnapshotStore

AS_OF = date(2026, 10, 26)


# ── 1. 一致预期挂真实快照编号 ──────────────────────────────────────

def test_一致预期事实挂FMP快照编号_能反查原始响应(tmp_path, monkeypatch):
    from ir_agent.sources import fmp
    store = SnapshotStore(tmp_path)
    rows = [{"date": "2026-09-26", "epsAvg": 8.5, "epsLow": 8, "epsHigh": 9, "numAnalystsEps": 30},
            {"date": "2027-09-25", "epsAvg": 9.4, "epsLow": 9, "epsHigh": 10, "numAnalystsEps": 28}]
    sid = store.save(source="fmp_analyst_estimates_annual", payload=rows, url="u",
                     fetched_at=datetime(2026, 10, 26, 9), params={"symbol": "AAPL"})
    cs = fmp.fetch_consensus("AAPL", "0927", AS_OF, store, today=AS_OF)
    assert cs.source_id == sid
    store.load(cs.source_id)                                   # 不抛 = 能反查


def test_美股外部资料用一致预期的快照编号落账(tmp_path, monkeypatch):
    from ir_agent import full
    from ir_agent.sources import fmp, fomc, peer_filings
    from ir_agent.sources.reports import Consensus, Estimate
    store = SnapshotStore(tmp_path)
    sub = store.save(source="sec_submissions", payload={"fiscalYearEnd": "0927"}, url="u", fetched_at=datetime.now())
    cs = Consensus(as_of=AS_OF, window_days=0, stat="均值", provider="FMP", source_id="fmp_snap_1",
                   eps={2027: Estimate(fiscal_year=2027, n=30, median=D("8.5"), low=D(8), high=D(9))})
    monkeypatch.setattr(fmp, "fetch_consensus", lambda *a, **k: cs)
    monkeypatch.setattr(fomc, "fetch_statements", lambda as_of, st: ([], ""))
    monkeypatch.setattr(peer_filings, "peer_10k_docs", lambda r, as_of: ([], []))
    r = SimpleNamespace(ledger=FactLedger(), store=store, sec_meta={"submissions_sid": sub})
    full._load_external_us("AAPL", r, AS_OF)
    sids = {f.source_id for (k, _p), fs in r.ledger._facts.items() if k.startswith("consensus.") for f in fs}
    assert sids == {"fmp_snap_1"}


# ── 2. 外部行业规模挂它自己引用的那篇文档 ─────────────────────────

def _doc(doc_id, url, text, tier=Tier.MEDIA, publisher="IDC"):
    return Document(doc_id=doc_id, url=url, title=doc_id, publisher=publisher, tier=tier,
                    published=date(2026, 10, 8), retrieved=AS_OF, text=text)


def test_外部行业规模的来源是它引用的文档_不是语料库第一篇(tmp_path):
    from ir_agent import full
    from ir_agent.basis import Basis
    from ir_agent.value_chain import SizeClaim
    store = SnapshotStore(tmp_path)
    corpus = Corpus(as_of=AS_OF)
    corpus.add(_doc("fomc_20260916", "https://fed/x", "The Committee decided", tier=Tier.OFFICIAL))
    corpus.add(_doc("ow_idc", "https://idc.com/pc", "PC shipments totaled 61.2 million units in 3Q26"))
    size = SizeClaim(label="全球 PC 出货", value=D("61.2"), unit="百万台", period="2026Q3S",
                     bases=[Basis.report("PC shipments totaled 61.2 million units in 3Q26")])
    sid, why = full._size_source(size, corpus, store)
    assert why == "" and sid != "fomc_20260916"
    snap = store.load(sid)
    assert snap.url == "https://idc.com/pc" and "61.2 million" in snap.payload["text"]


def test_外部行业规模找不到引用文档时不入账并说明(tmp_path):
    from ir_agent import full
    from ir_agent.basis import Basis
    from ir_agent.value_chain import SizeClaim
    corpus = Corpus(as_of=AS_OF)
    corpus.add(_doc("fomc_20260916", "https://fed/x", "The Committee decided", tier=Tier.OFFICIAL))
    size = SizeClaim(label="x", value=D(1), unit="u", period="2026Q3S", bases=[Basis.report("nowhere")])
    sid, why = full._size_source(size, corpus, SnapshotStore(tmp_path))
    assert sid is None and "找不到" in why


# ── 3. 来源清单 ───────────────────────────────────────────────────

def test_账本事实按快照分组_标条数并给出本地快照文件(tmp_path):
    from ir_agent.sources.manifest import collect_rows
    store = SnapshotStore(tmp_path)
    sid = store.save(source="sec_companyfacts", payload={}, url="u", fetched_at=datetime.now())
    led = FactLedger()
    for p in ("2007FY", "2025FY"):
        led.put(Fact(key="revenue", value=D(1), unit="美元", period=p, as_of=date(2025, 10, 31),
                     source_id=sid, method=Method.REPORTED))
    rows = [r for r in collect_rows(led, store=store) if r.kind == "账本事实"]
    assert len(rows) == 1
    assert "2 条" in rows[0].label and rows[0].local == str(store.path_for(sid))
    assert Path(rows[0].local).exists()


def test_同业样本出处按市场写_美股不写东财():
    from ir_agent.sources.manifest import collect_rows
    us = [r for r in collect_rows(FactLedger(), peers=("A",) * 20, code="AAPL") if r.kind == "同业样本"]
    cn = [r for r in collect_rows(FactLedger(), peers=("A",) * 20, code="600183") if r.kind == "同业样本"]
    assert "Fama-French" in us[0].origin and "东财" not in us[0].origin
    assert cn[0].origin == "东财行业分类"


def test_美股年报一节写10K并指向本地快照(tmp_path):
    from ir_agent.sources.manifest import collect_rows, render
    store = SnapshotStore(tmp_path)
    sid = store.save(source="sec_10k", payload="<html/>", url="https://sec/aapl-20250927.htm", fetched_at=datetime.now())
    rows = collect_rows(FactLedger(), annual=("https://sec/aapl-20250927.htm", store.path_for(sid)), code="AAPL")
    ann = [r for r in rows if r.kind.startswith("年报")]
    assert ann[0].kind == "年报（10-K）" and ann[0].local == str(store.path_for(sid)) and ann[0].note == ""
    assert "## 年报（10-K）" in render(rows, []) and "年报 PDF" not in render(rows, [])


def test_同业10K与电话会纪要各自成节_不混进公司公告():
    from ir_agent.sources.manifest import collect_rows, render
    c = Corpus(as_of=AS_OF)
    c.add(_doc("sec8k_1", "https://sec/8k", "x", tier=Tier.COMPANY, publisher="Apple Inc."))
    c.add(Document(doc_id="peer10k_CSCO_1", url="https://sec/csco", title="CSCO 10-K", publisher="CISCO（同业 10-K）",
                   tier=Tier.COMPANY, published=date(2026, 9, 2), retrieved=AS_OF, text="x", channel="peer_filing"))
    c.add(Document(doc_id="defeatbeta_AAPL_2026Q3", url="https://hf/x", title="纪要", publisher="defeatbeta（二手）",
                   tier=Tier.COMPANY, published=date(2026, 7, 30), retrieved=AS_OF, text="x", channel="transcript"))
    by = {}
    for r in collect_rows(FactLedger(), corpus=c):
        by.setdefault(r.kind, []).append(r.origin)
    assert by["公司公告"] == ["https://sec/8k"]
    assert by["同业披露（竞争对手 10-K）"] == ["https://sec/csco"]
    assert by["电话会纪要（二手转存）"] == ["https://hf/x"]
    md = render(collect_rows(FactLedger(), corpus=c), [])
    assert "## 同业披露（竞争对手 10-K）（1 项）" in md and "## 电话会纪要（二手转存）（1 项）" in md


def test_文档渠道字段随落盘读回():
    from ir_agent.sources.openweb import _doc_from_json, _doc_to_json
    d = Document(doc_id="x", url="u", title="t", publisher="p", tier=Tier.COMPANY, published=AS_OF,
                 retrieved=AS_OF, text="x", channel="transcript")
    assert _doc_from_json(_doc_to_json(d)).channel == "transcript"
    raw = _doc_to_json(d)
    del raw["channel"]
    assert _doc_from_json(raw).channel == ""                   # 旧落盘文件


def test_纪要文档带渠道标记(tmp_path, monkeypatch):
    from ir_agent.sources import transcripts
    monkeypatch.setattr(transcripts, "_query", lambda sym: [
        {"symbol": "AAPL", "fiscal_year": 2026, "fiscal_quarter": 3, "report_date": "2026-07-30",
         "transcripts": [{"paragraph_number": 1, "speaker": "Tim", "content": "hi"}]}])
    docs, _ = transcripts.fetch_transcripts("AAPL", AS_OF, SnapshotStore(tmp_path))
    assert [d.channel for d in docs] == ["transcript"]
