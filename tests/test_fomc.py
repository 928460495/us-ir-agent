from datetime import date
from pathlib import Path

import pytest

from ir_agent.sources import fomc
from ir_agent.sources.external import Tier
from ir_agent.sources.snapshot import SnapshotStore

FIX = Path(__file__).parent / "fixtures" / "fomc"


@pytest.fixture
def fake(monkeypatch):
    seen = []

    def get(url):
        seen.append(url)
        if url.endswith("fomccalendars.htm"):
            return (FIX / "fomccalendars.htm").read_text(encoding="utf-8")
        return (FIX / "monetary20260916a.htm").read_text(encoding="utf-8")
    monkeypatch.setattr(fomc, "_get", get)
    return seen


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path)


def test_取as_of之前最近两份声明(fake, store):
    docs, err = fomc.fetch_statements(date(2026, 10, 25), store)
    assert err == "" and [d.published for d in docs] == [date(2026, 9, 16), date(2026, 7, 29)]
    assert docs[0].tier is Tier.OFFICIAL and docs[0].publisher == "美联储（FOMC）"
    assert docs[0].doc_id == "fomc_20260916" and docs[0].title == "FOMC 政策声明"


def test_晚于as_of的声明不取(fake, store):
    docs, _ = fomc.fetch_statements(date(2026, 9, 15), store)
    assert [d.published for d in docs] == [date(2026, 7, 29), date(2026, 6, 17)]


def test_只认声明链接_不认实施说明与纪要(fake, store):
    fomc.fetch_statements(date(2026, 10, 25), store)
    assert not any(u.endswith("a1.htm") or "minutes" in u for u in fake)


def test_正文取自article区域(fake, store):
    d = fomc.fetch_statements(date(2026, 10, 25), store)[0][0]
    assert "The Federal Open Market Committee approved the following statement" in d.text
    assert "<div" not in d.text


def test_原始页面落快照(fake, tmp_path):
    fomc.fetch_statements(date(2026, 10, 25), SnapshotStore(tmp_path))
    assert any(p.name.startswith("fomc_statement") for p in tmp_path.iterdir())


def test_日历之前的时点给说明(fake, store):
    docs, err = fomc.fetch_statements(date(2024, 6, 1), store)
    assert docs == [] and "覆盖" in err


def test_网络失败给说明不抛(monkeypatch, store):
    def boom(url):
        raise OSError("timeout")
    monkeypatch.setattr(fomc, "_get", boom)
    docs, err = fomc.fetch_statements(date(2026, 10, 25), store)
    assert docs == [] and "FOMC" in err


def _run(tmp_path):
    from datetime import datetime as dt
    from types import SimpleNamespace
    from ir_agent.ledger import FactLedger
    from ir_agent.sources.external import Corpus
    st = SnapshotStore(tmp_path / "snap")
    sid = st.save(source="sec_submissions", payload={"cik": "320193", "fiscalYearEnd": "0927"},
                  url="u", fetched_at=dt.now())
    old = Corpus(as_of=date(2026, 10, 25))
    return SimpleNamespace(code="AAPL", ledger=FactLedger(), store=st, corpus=old, consensus=None,
                           sec_meta={"submissions_sid": sid})


def test_美股外部资料放入FOMC声明且语料库重建(fake, monkeypatch, tmp_path):
    from ir_agent import full
    from ir_agent.sources import fmp
    monkeypatch.setattr(fmp, "fetch_consensus", lambda *a, **k: None)
    r = _run(tmp_path)
    old = r.corpus
    full._load_external("AAPL", r, date(2026, 10, 25))
    assert r.corpus is not old                                   # 两个阶段都跑：每次重建，不重复入库
    assert [d.doc_id for d in r.corpus.docs] == ["fomc_20260916", "fomc_20260729"]


def test_FOMC取不到时说明(monkeypatch, tmp_path):
    from ir_agent import full
    from ir_agent.sources import fmp
    monkeypatch.setattr(fmp, "fetch_consensus", lambda *a, **k: None)
    monkeypatch.setattr(fomc, "fetch_statements", lambda as_of, store: ([], "FOMC 声明（会议日历取不到：OSError）"))
    _, _, err = full._load_external("AAPL", _run(tmp_path), date(2026, 10, 25))
    assert "FOMC 声明" in err
