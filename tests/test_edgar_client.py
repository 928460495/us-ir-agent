import json
import time
from pathlib import Path

import pytest

from ir_agent.sources import edgar
from ir_agent.sources.snapshot import SnapshotStore

FIX = Path(__file__).parent / "fixtures" / "edgar"


class _Resp:
    def __init__(self, body: bytes, status: int = 200):
        self.content, self.status_code = body, status

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}", response=self)


def _route(url: str) -> bytes:
    if url.endswith("company_tickers_exchange.json"):
        return (FIX / "company_tickers_exchange.json").read_bytes()
    name = url.rsplit("/", 1)[-1]                      # CIK0000320193.json
    kind = "submissions" if "/submissions/" in url else "companyfacts"
    p = FIX / f"{kind}_{name[3:13]}.json"
    if p.exists():
        return p.read_bytes()
    return json.dumps({"cik": int(name[3:13]), "facts": {}}).encode()


@pytest.fixture
def calls():
    return []


@pytest.fixture
def fake_get(monkeypatch, calls):
    monkeypatch.setenv("SEC_USER_AGENT", "Tester tester@example.org")

    def get(url, headers=None, timeout=None):
        calls.append((url, headers, time.monotonic()))
        return _Resp(_route(url))
    monkeypatch.setattr(edgar.requests, "get", get)
    return get


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path)


def test_没有身份就不发请求(monkeypatch, store):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    called = []
    monkeypatch.setattr(edgar.requests, "get", lambda *a, **k: called.append(1))
    with pytest.raises(edgar.SecIdentityMissing, match="export SEC_USER_AGENT"):
        edgar.resolve_ticker("AAPL", store)
    assert called == []


def test_请求头带环境变量里的身份(fake_get, calls, store):
    edgar.resolve_ticker("AAPL", store)
    assert calls[0][1]["User-Agent"] == "Tester tester@example.org"


def test_代码解析出补零的CIK(fake_get, store):
    info = edgar.resolve_ticker("aapl", store)
    assert info == edgar.TickerInfo(cik="0000320193", name="Apple Inc.", exchange="Nasdaq")


def test_点号与连字符代码等价(fake_get, store):
    assert edgar.resolve_ticker("BRK.B", store).cik == edgar.resolve_ticker("BRK-B", store).cik == "0001067983"


def test_未知代码报错(fake_get, store):
    with pytest.raises(edgar.UnknownTicker, match="ZZZZ"):
        edgar.resolve_ticker("ZZZZ", store)


def test_原始响应落快照(fake_get, tmp_path):
    doc, sid = edgar.fetch_companyfacts("0000320193", SnapshotStore(tmp_path))
    assert (tmp_path / f"{sid}.json").exists()
    assert SnapshotStore(tmp_path).load(sid).payload == doc


def test_submissions取行业与财年末(fake_get, store):
    doc, _ = edgar.fetch_submissions("0000320193", store)
    assert doc["sic"] == "3571" and doc["fiscalYearEnd"] == "0927"


def test_CIK参数会被补零(fake_get, calls, store):
    edgar.fetch_submissions("320193", store)
    assert calls[-1][0].endswith("/submissions/CIK0000320193.json")


def test_限流不超过每秒九次(fake_get, calls, store):
    for _ in range(20):
        edgar.fetch_submissions("0000320193", store)
    ts = [c[2] for c in calls]
    assert min(b - a for a, b in zip(ts, ts[1:])) >= 1 / 9 - 1e-3
