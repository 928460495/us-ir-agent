"""同业财报逐家取数：与本司同一条解析链，按分析时点缓存，快照裁剪到映射表科目。不联网。"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
import requests

from ir_agent.sources import edgar, us_peers
from ir_agent.sources.edgar_facts import MAPPED_TAGS, parse_companyfacts, period_ends, slim
from ir_agent.sources.industry import Member
from ir_agent.sources.snapshot import SnapshotStore

FIX = Path(__file__).parent / "fixtures" / "edgar"
AS_OF = date(2026, 10, 26)
UA = "Tester tester@example.com"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _key(f):
    return (f.key, f.period, f.value, f.as_of)


def test_裁剪后解析结果与全量一致():
    doc = _load("companyfacts_0000320193.json")
    doc["facts"]["us-gaap"]["NotMappedAnywhere"] = {"units": {"USD": []}}
    full = parse_companyfacts(doc, "0927", "s")
    small = slim(doc)
    assert sorted(map(_key, parse_companyfacts(small, "0927", "s"))) == sorted(map(_key, full))
    assert set(small["facts"]["us-gaap"]) <= MAPPED_TAGS
    assert "NotMappedAnywhere" not in small["facts"]["us-gaap"]
    assert small["facts"]["dei"] == doc["facts"]["dei"]


def test_期末日映射():
    ends = period_ends(_load("companyfacts_0000320193.json"), "0927")
    assert ends["2025FY"] == date(2025, 9, 27)
    assert ends["2024FY"] == date(2024, 9, 28)


class _R:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError(f"{self.status_code} Client Error")


SUB = {"cik": "1", "name": "甲公司", "fiscalYearEnd": "0927", "sic": "3571",
       "filings": {"recent": {"accessionNumber": ["x"] * 3}}}


def _fake_sec(monkeypatch, routes, calls):
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    monkeypatch.setattr(edgar, "_MIN_INTERVAL", 0)

    def get(url, headers=None, timeout=None, params=None):
        calls.append(url)
        for frag, body in routes.items():
            if frag in url:
                return body if isinstance(body, _R) else _R(body)
        return _R({}, 404)
    monkeypatch.setattr(edgar.requests, "get", get)


def _write_index(store, url, source, fetched: datetime, payload):
    sid = store.save(source=source, payload=payload, url=url, fetched_at=fetched)
    edgar._cache_index_put(store, source, url, fetched, sid)
    return sid


def test_缓存在分析时点当天及之后抓的才复用(tmp_path, monkeypatch):
    store, calls = SnapshotStore(tmp_path), []
    url = edgar.COMPANYFACTS_URL.format(cik="0000000001")
    _write_index(store, url, "sec_companyfacts", datetime(2026, 10, 27, 9), {"facts": {"us-gaap": {}}})
    _fake_sec(monkeypatch, {"companyfacts": {"facts": {"us-gaap": {}}, "fresh": True}}, calls)
    doc, _ = edgar.fetch_json_cached(url, "sec_companyfacts", store, date(2026, 10, 26))
    assert calls == [] and "fresh" not in doc
    doc, _ = edgar.fetch_json_cached(url, "sec_companyfacts", store, date(2026, 10, 28))
    assert len(calls) == 1 and doc["fresh"] is True


def test_一家只报IFRS一家HTTP失败_其余照常且逐家留说明(tmp_path, monkeypatch):
    store, calls, notes = SnapshotStore(tmp_path), [], []
    good = _load("companyfacts_0000320193.json")
    monkeypatch.setattr(edgar, "load_tickers", lambda st: ({
        "AAA": edgar.TickerInfo("0000000001", "甲", "Nasdaq"),
        "IFRS": edgar.TickerInfo("0000000002", "乙", "NYSE"),
        "BAD": edgar.TickerInfo("0000000003", "丙", "NYSE")}, "sid"))
    _fake_sec(monkeypatch, {
        "submissions/CIK0000000001": SUB, "submissions/CIK0000000002": dict(SUB, fiscalYearEnd="1231"),
        "submissions/CIK0000000003": _R({}, 500),
        "companyfacts/CIK0000000001": good,
        "companyfacts/CIK0000000002": {"facts": {"ifrs-full": {"Revenue": {}}}}}, calls)
    monkeypatch.setattr(edgar, "with_retry", lambda fn: fn())
    books = us_peers.load_peer_books([Member("AAA", "甲"), Member("IFRS", "乙"), Member("BAD", "丙")],
                                     store, AS_OF, notes)
    assert set(books) == {"AAA"}
    b = books["AAA"]
    assert b.fye == "0927" and b.ends["2025FY"] == date(2025, 9, 27)
    assert any(f.key == "revenue" and f.period == "2025FY" for f in b.facts)
    assert any(n.startswith("IFRS 乙") and "us-gaap" in n for n in notes)
    assert any(n.startswith("BAD 丙") and "HTTPError" in n for n in notes)


def test_快照里没有邮箱且申报清单被裁掉(tmp_path, monkeypatch):
    store, calls, notes = SnapshotStore(tmp_path), [], []
    monkeypatch.setattr(edgar, "load_tickers", lambda st: (
        {"AAA": edgar.TickerInfo("0000000001", "甲", "Nasdaq")}, "sid"))
    _fake_sec(monkeypatch, {"submissions": SUB, "companyfacts": _load("companyfacts_0000320193.json")}, calls)
    us_peers.load_peer_books([Member("AAA", "甲")], store, AS_OF, notes)
    texts = [p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json")]
    assert texts and not any("tester@example.com" in t for t in texts)
    subs = [json.loads(t) for t in texts if '"source": "sec_submissions"' in t]
    assert subs and all("filings" not in s["payload"] for s in subs)
