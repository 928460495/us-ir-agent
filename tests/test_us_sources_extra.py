"""美股补源（用户 2026-10-26 决定）：同业 10-K 进语料库（COMPANY 级）、FF49 行业官方统计与
地区联储制造业调查入账本。不联网。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from ir_agent.sources import edgar, fred, peer_filings
from ir_agent.sources.external import Corpus, Tier
from ir_agent.sources.snapshot import SnapshotStore
from ir_agent.sources.us_peers import PeerBook

AS_OF = date(2026, 10, 26)
FIX = Path(__file__).parent / "fixtures" / "edgar"
TENK = (FIX / "aapl_10k_excerpt.htm").read_text(encoding="utf-8")


def _sub(acc="0000858877-25-000099", filed="2025-09-05", report="2025-07-26", form="10-K"):
    return {"name": "CISCO", "fiscalYearEnd": "0726", "filings": {"recent": {
        "accessionNumber": [acc], "form": [form], "filingDate": [filed], "reportDate": [report],
        "primaryDocument": ["csco-20250726.htm"]}}}


@pytest.fixture
def r(tmp_path):
    store = SnapshotStore(tmp_path)
    sid = store.save(source="sec_submissions_slim", payload=_sub(), url="u", fetched_at=datetime.now())
    book = PeerBook(ticker="CSCO", name="CISCO SYSTEMS, INC.", cik="0000858877", fye="0726", facts=[],
                    ends={}, sids={"submissions": sid})
    return SimpleNamespace(store=store, comps_codes=["CSCO"], peer_books={"CSCO": book})


def test_可比同业的10K章节进语料库_COMPANY级且标明同业(r, monkeypatch):
    seen = []
    monkeypatch.setattr(edgar, "fetch_text_cached",
                        lambda url, source, store, as_of: (seen.append(url) or TENK, "sid10k"))
    docs, notes = peer_filings.peer_10k_docs(r, AS_OF)
    assert notes == [] and len(docs) == 1
    d = docs[0]
    assert d.tier is Tier.COMPANY and "同业" in d.publisher and "CISCO" in d.publisher
    assert d.channel == "peer_filing"
    assert d.published == date(2025, 9, 5) and d.retrieved == AS_OF
    assert "csco-20250726.htm" in d.url and seen == [d.url]
    assert d.text and d.prompt_limit == peer_filings.PROMPT_LIMIT
    c = Corpus(as_of=AS_OF)
    assert c.add_if_visible(d)


def test_时点之后的10K不取_没有可比时不取(r, monkeypatch):
    r.store.save(source="x", payload={}, url="u", fetched_at=datetime.now())
    r.peer_books["CSCO"].sids["submissions"] = r.store.save(
        source="sec_submissions_slim", payload=_sub(filed="2026-11-05"), url="u", fetched_at=datetime.now())
    monkeypatch.setattr(edgar, "fetch_text_cached", lambda *a, **k: pytest.fail("不该取"))
    docs, notes = peer_filings.peer_10k_docs(r, AS_OF)
    assert docs == [] and any("CSCO" in n and "10-K" in n for n in notes)
    assert peer_filings.peer_10k_docs(SimpleNamespace(comps_codes=[], peer_books={}), AS_OF) == ([], [])


def test_取数失败逐家说明不抛(r, monkeypatch):
    monkeypatch.setattr(edgar, "fetch_text_cached",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    docs, notes = peer_filings.peer_10k_docs(r, AS_OF)
    assert docs == [] and any("CSCO" in n and "RuntimeError" in n for n in notes)


def test_地区联储制造业调查进宏观登记表_属领先指标():
    keys = {s.key: s for s in fred.US_MACRO}
    assert keys["macro.philly_fed_mfg"].series_id == "GACDFSA066MSFRBPHI"
    assert keys["macro.empire_state_mfg"].series_id == "GACDISA066MSFRBNY"
    assert fred.classify("macro.philly_fed_mfg") == fred.LEADING
    shown = dict(fred.UsMacroSnapshot._SHOWN)
    assert "macro.philly_fed_mfg" in shown and "macro.empire_state_mfg" in shown
    assert not any(ch.isdigit() for k in ("macro.philly_fed_mfg", "macro.empire_state_mfg") for ch in shown[k])


def test_FF49行业官方统计_四个试跑行业有序列_其他行业为空():
    for num in (21, 35, 37, 43):
        specs = fred.industry_series(num)
        assert specs and all(s.key.startswith("macro.ind_") and s.transform == "yoy" for s in specs)
    assert fred.industry_series(1) == ()
    assert {s.series_id for s in fred.industry_series(35)} == {"A34SVS", "IPG334S"}


def test_行业序列按当时版本取并入账(monkeypatch, tmp_path):
    obs = [{"date": f"{y}-08-01", "value": v, "realtime_start": f"{y}-10-01", "realtime_end": "9999-12-31"}
           for y, v in ((2025, "100"), (2026, "110"))]
    monkeypatch.setattr(fred, "_observations", lambda sid, as_of, store: (obs, "fred_a34svs"))
    fs = fred.fetch_series_asof("A34SVS", AS_OF, SnapshotStore(tmp_path))
    yoy = [f for f in fs if f.key == "macro.ind_computer_shipments_yoy"]
    assert len(yoy) == 1 and yoy[0].value == D("0.1") and yoy[0].period == "2026-08"
    assert yoy[0].as_of == date(2026, 10, 1)


def _prompt_seen(monkeypatch, code):
    from ir_agent.sources import openweb
    seen = []

    def client(prompt):
        seen.append(prompt)
        return ""
    openweb.gather(client, code=code, company="X", industry="Y", as_of=AS_OF, corpus=Corpus(as_of=AS_OF))
    return seen[0]


def test_美股开放网络检索提示_美国官方来源与新闻稿转载渠道(monkeypatch):
    p = _prompt_seen(monkeypatch, "AAPL")
    assert "美股" in p and "A 股" not in p and "统计局" not in p
    assert "Census" in p and "Business Wire" in p and "PR Newswire" in p


def test_A股开放网络检索提示逐字不变(monkeypatch):
    from ir_agent.sources import openweb
    p = _prompt_seen(monkeypatch, "600183")
    assert p == openweb.PROMPT.format(company="X", code="600183", industry="Y",
                                      per_slot=openweb.PER_SLOT, as_of=AS_OF.isoformat())


def test_开放网络不抓sec_gov_申报文件已由EDGAR入库(monkeypatch):
    from ir_agent.sources import openweb
    called = []
    monkeypatch.setattr(openweb, "fetch", lambda url, **k: called.append(url))
    line = ("候选: 公司重大动态 | 8-K | https://www.sec.gov/Archives/edgar/data/320193/x/a8-k.htm"
            " | Apple | 2026-07-30")
    res = openweb.gather(lambda p: line, code="AAPL", company="Apple", industry="x", as_of=AS_OF,
                         corpus=Corpus(as_of=AS_OF))
    assert called == [] and res.fetch_failed == 0


def test_美股检索提示说明不要sec_gov申报与PDF(monkeypatch):
    p = _prompt_seen(monkeypatch, "AAPL")
    assert "sec.gov" in p and "PDF" in p
