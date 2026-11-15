"""USAspending 近 12 个月联邦合同义务金额（OFFICIAL，用户 2026-11-15 决定）。不联网。"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
import requests

from ir_agent.ledger import FactLedger, Method
from ir_agent.sources import usaspending
from ir_agent.sources.snapshot import SnapshotStore
from ir_agent.sources.usaspending import fetch_contracts

AS_OF = date(2026, 11, 15)


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


def _stub(monkeypatch, rows=None, status=200, exc=None):
    calls = []

    def post(url, **kw):
        calls.append((url, kw))
        if exc is not None:
            raise exc
        return _Resp({"category": "recipient", "results": rows or []}, status)
    monkeypatch.setattr(usaspending.requests, "post", post)
    return calls


def test_合计同名承接方的联邦合同义务金额(monkeypatch, tmp_path):
    _stub(monkeypatch, [{"name": "PALANTIR USG INC", "amount": 1753103159.6},
                        {"name": "PALANTIR TECHNOLOGIES INC.", "amount": 575419256.94},
                        {"name": "PALANTIRX LLC", "amount": 1}])
    store = SnapshotStore(tmp_path)
    facts, notes = fetch_contracts("Palantir Technologies Inc.", AS_OF, store)
    f = facts[0]
    assert f.key == "gov.contract_obligations_ttm" and f.value == D("2328522416.54")
    assert f.unit == "美元" and f.currency == "USD" and f.method is Method.REPORTED
    assert f.period == "2026-11-15" and f.as_of == AS_OF
    assert store.load(f.source_id).source == "usaspending_recipient"
    assert "PALANTIR USG INC" in notes[0] and "PALANTIRX" not in notes[0]


def test_撞名不纳入(monkeypatch, tmp_path):
    _stub(monkeypatch, [{"name": "APPLE INC", "amount": 10},
                        {"name": "APPLE HOSPITALITY REIT", "amount": 99}])
    facts, notes = fetch_contracts("Apple Inc.", AS_OF, SnapshotStore(tmp_path))
    assert facts[0].value == D(10) and "HOSPITALITY" not in notes[0]


def test_核心词不足四个字母取前两个词(monkeypatch, tmp_path):
    calls = _stub(monkeypatch, [{"name": "SAP NATIONAL SECURITY SERVICES INC", "amount": 5},
                                {"name": "IBM CORP", "amount": 7}])
    fetch_contracts("IBM Corp", AS_OF, SnapshotStore(tmp_path))
    assert calls[0][1]["json"]["filters"]["recipient_search_text"] == ["IBM"]
    assert usaspending.core_words("Sap Se National Security Inc.") == ["SAP", "SE"]


def test_请求窗口近十二个月且只要合同类(monkeypatch, tmp_path):
    calls = _stub(monkeypatch, [])
    fetch_contracts("Palantir Technologies Inc.", AS_OF, SnapshotStore(tmp_path))
    url, kw = calls[0]
    assert url == usaspending.URL
    flt = kw["json"]["filters"]
    assert flt["award_type_codes"] == ["A", "B", "C", "D"]
    assert flt["time_period"] == [{"start_date": "2025-11-15", "end_date": "2026-11-15"}]
    assert kw["json"]["limit"] == 50


def test_接口失败记说明不抛(monkeypatch, tmp_path):
    _stub(monkeypatch, exc=requests.ConnectionError("boom"))
    facts, notes = fetch_contracts("Palantir Technologies Inc.", AS_OF, SnapshotStore(tmp_path))
    assert facts == [] and "USAspending" in notes[0]
    _stub(monkeypatch, status=500)
    facts, notes = fetch_contracts("Palantir Technologies Inc.", AS_OF, SnapshotStore(tmp_path))
    assert facts == [] and "500" in notes[0]


def test_没有合同入账零并说明(monkeypatch, tmp_path):
    _stub(monkeypatch, [{"name": "APPLE HOSPITALITY REIT", "amount": 99}])
    facts, notes = fetch_contracts("Apple Inc.", AS_OF, SnapshotStore(tmp_path))
    assert facts[0].value == 0 and "近 12 个月无联邦合同" in notes[0]


def test_请求不带key与邮箱(monkeypatch, tmp_path):
    monkeypatch.setenv("SEC_USER_AGENT", "Someone someone@example.com")
    calls = _stub(monkeypatch, [])
    fetch_contracts("Palantir Technologies Inc.", AS_OF, SnapshotStore(tmp_path))
    blob = json.dumps(calls[0][1], default=str)
    assert "@" not in blob and "key" not in blob.lower()


def test_外部资料加载器写入联邦合同_公司名取不到时不请求(monkeypatch, tmp_path):
    from ir_agent import full
    from ir_agent.sources import fmp, fomc, peer_filings
    monkeypatch.setattr(fmp, "fetch_consensus", lambda *a, **k: None)
    monkeypatch.setattr(fomc, "fetch_statements", lambda as_of, st: ([], ""))
    monkeypatch.setattr(peer_filings, "peer_10k_docs", lambda r, as_of: ([], []))
    store = SnapshotStore(tmp_path)

    def run(payload):
        sub = store.save(source="sec_submissions", payload=payload, url="u", fetched_at=datetime.now())
        r = SimpleNamespace(ledger=FactLedger(), store=store, sec_meta={"submissions_sid": sub})
        _md, _p, notes = full._load_external_us("PLTR", r, AS_OF)
        return r, notes

    calls = _stub(monkeypatch, [{"name": "PALANTIR USG INC", "amount": 3}])
    r, notes = run({"fiscalYearEnd": "1231", "name": "Palantir Technologies Inc."})
    assert r.ledger.get("gov.contract_obligations_ttm", "2026-11-15", AS_OF).value == D(3)
    assert "PALANTIR USG INC" in notes and len(calls) == 1
    r, notes = run({"fiscalYearEnd": "1231"})
    assert len(calls) == 1 and "USAspending" in notes


def test_联邦合同挂自己的期间():
    from ir_agent import full
    assert "gov.contract_obligations_ttm".startswith(full._CONTEXT_PREFIXES)
