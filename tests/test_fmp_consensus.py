from datetime import date
from decimal import Decimal as D

import pytest

from ir_agent import corpus as C
from ir_agent.sources import fmp
from ir_agent.sources.reports import consensus_facts
from ir_agent.sources.snapshot import SnapshotStore

TODAY = date(2026, 10, 25)
ROWS = [
    {"symbol": "AAPL", "date": "2030-09-27", "epsAvg": 12.495, "epsHigh": 13.9, "epsLow": 11.6, "numAnalystsEps": 1},
    {"symbol": "AAPL", "date": "2027-09-25", "epsAvg": 9.1, "epsHigh": 9.8, "epsLow": 8.6, "numAnalystsEps": 30},
    {"symbol": "AAPL", "date": "2026-09-26", "epsAvg": 8.25, "epsHigh": 8.5, "epsLow": 8.0, "numAnalystsEps": 35},
]


class R:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        return self._b


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path)


def _fake(monkeypatch, body=ROWS, status=200):
    monkeypatch.setenv("FMP_API_KEY", "k")
    monkeypatch.setattr(fmp.requests, "get", lambda url, params=None, timeout=None: R(body, status))


def test_按财年解析均值高低与人数(monkeypatch, store):
    _fake(monkeypatch)
    c = fmp.fetch_consensus("AAPL", "0927", TODAY, store, today=TODAY)
    assert sorted(c.eps) == [2026, 2027]
    e = c.eps[2027]
    assert (e.n, e.median, e.low, e.high) == (30, D("9.1"), D("8.6"), D("9.8"))
    assert c.stat == "均值" and "FMP" in c.provider


def test_单人覆盖的年份剔除(monkeypatch, store):
    _fake(monkeypatch)
    assert 2030 not in fmp.fetch_consensus("AAPL", "0927", TODAY, store, today=TODAY).eps


def test_回测时点拿不到当时的预期(monkeypatch, store):
    _fake(monkeypatch)
    assert fmp.fetch_consensus("AAPL", "0927", date(2026, 10, 21), store, today=TODAY) is None


def test_402返回None(monkeypatch, store):
    _fake(monkeypatch, body="Premium", status=402)
    assert fmp.fetch_consensus("CAT", "1231", TODAY, store, today=TODAY) is None


def test_一致预期入账用美元(monkeypatch, store):
    _fake(monkeypatch)
    c = fmp.fetch_consensus("AAPL", "0927", TODAY, store, today=TODAY)
    fs = {f.key: f for f in consensus_facts(c, source_id="fmp", money_unit="美元") if f.period == "2027FY"}
    assert fs["consensus.eps"].unit == "美元" and fs["consensus.eps"].value == D("9.1")


def test_提示词写均值与来源不写券商研报(monkeypatch, store):
    _fake(monkeypatch)
    c = fmp.fetch_consensus("AAPL", "0927", TODAY, store, today=TODAY)
    p, md = C.consensus_prompt(c), C.consensus_md(c)
    assert "EPS 均值 [[consensus.eps@2027FY]]" in p and "研报" not in p
    assert "FMP" in md and "研报" not in md


def test_A股一致预期措辞不变():
    from ir_agent.sources.reports import Consensus, Estimate
    c = Consensus(as_of=TODAY, window_days=90, n_reports=5,
                  eps={2026: Estimate(2026, 3, D("1"), D("0.9"), D("1.1"))})
    assert "EPS 中位" in C.consensus_prompt(c) and "篇研报" in C.consensus_md(c)


@pytest.fixture(autouse=True)
def _no_fomc(monkeypatch):
    from ir_agent.sources import fomc
    monkeypatch.setattr(fomc, "fetch_statements", lambda as_of, store: ([], ""))


def _run(tmp_path, code="AAPL"):
    from datetime import datetime
    from types import SimpleNamespace
    from ir_agent.ledger import FactLedger
    store = SnapshotStore(tmp_path / "snap")
    sid = store.save(source="sec_submissions", payload={"cik": "320193", "fiscalYearEnd": "0927"},
                     url="u", fetched_at=datetime.now())
    return SimpleNamespace(code=code, ledger=FactLedger(), store=store, corpus=None,
                           consensus=None, sec_meta={"submissions_sid": sid})


def test_美股外部资料取FMP一致预期不取东财研报(monkeypatch, tmp_path):
    from ir_agent import full
    monkeypatch.setattr(C, "gather", lambda **k: (_ for _ in ()).throw(AssertionError("东财")))
    cs = fmp_c(monkeypatch, tmp_path)
    monkeypatch.setattr(fmp, "fetch_consensus", lambda t, fye, as_of, store: cs)
    r = _run(tmp_path)
    md, pr, err = full._load_external("AAPL", r, TODAY)
    assert r.ledger.get("consensus.eps", "2027FY", as_of=TODAY).unit == "美元"
    assert "均值" in pr and err == ""


def fmp_c(monkeypatch, tmp_path):
    _fake(monkeypatch)
    return fmp.fetch_consensus("AAPL", "0927", TODAY, SnapshotStore(tmp_path), today=TODAY)


def test_美股取不到一致预期时说明原因(monkeypatch, tmp_path):
    from ir_agent import full
    monkeypatch.setattr(fmp, "fetch_consensus", lambda t, fye, as_of, store: None)
    md, pr, err = full._load_external("CAT", _run(tmp_path, "CAT"), TODAY)
    assert "一致预期不可用" in err and "研报" not in err
