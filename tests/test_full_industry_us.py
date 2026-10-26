"""美股行业聚合与画像：同业年度数按期末就近重贴到本司财年，聚合与画像沿用 A 股代码。不联网。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from ir_agent import full
from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.sources import us_industry, us_peers, us_quotes
from ir_agent.sources.industry import Member, Peers
from ir_agent.sources.snapshot import SnapshotStore
from ir_agent.sources.us_industry import UsClassification
from ir_agent.sources.us_peers import PeerBook
from ir_agent.sources.us_quotes import Quote, QuoteUnavailable

AS_OF = date(2026, 10, 26)
AAPL_ENDS = {"2023FY": date(2023, 9, 30), "2024FY": date(2024, 9, 28), "2025FY": date(2025, 9, 27)}


def _f(key, period, v, as_of, sid="cf"):
    return Fact(key=key, value=D(str(v)), unit="美元", currency="USD", period=period, as_of=as_of,
                source_id=sid, method=Method.REPORTED)


def _cf_doc(ends: dict[str, date]):
    """只为 period_ends 推出本司期末日的最小 companyfacts。"""
    rows = [{"start": date(e.year - 1, e.month, e.day + 1 if e.day < 28 else 1).isoformat(),
             "end": e.isoformat(), "val": 1, "form": "10-K", "filed": "2025-10-31", "fy": 2025, "fp": "FY"}
            for e in ends.values()]
    return {"facts": {"us-gaap": {"Revenues": {"units": {"USD": rows}}}}}


def _book(t, fye, ends, rev: dict[str, int], filed_lag=50):
    facts = [_f("revenue", p, v, ends[p] + __import__("datetime").timedelta(days=filed_lag), sid=f"cf_{t}")
             for p, v in rev.items()]
    facts += [_f("equity_attr_parent", p, v // 2, ends[p] + __import__("datetime").timedelta(days=filed_lag))
              for p, v in rev.items()]
    return PeerBook(ticker=t, name=t.title(), cik="1", fye=fye, facts=facts, ends=ends)


DELL = _book("DELL", "0131", {"2025FY": date(2025, 1, 31), "2026FY": date(2026, 1, 30),
                             "2024FY": date(2024, 2, 2)}, {"2024FY": 88, "2025FY": 95, "2026FY": 100})
HPQ = _book("HPQ", "1031", {"2024FY": date(2024, 10, 31), "2025FY": date(2025, 10, 31)},
            {"2024FY": 53, "2025FY": 55})
SMCI = _book("SMCI", "0630", {"2025FY": date(2025, 6, 30), "2026FY": date(2026, 6, 30)},
             {"2025FY": 22, "2026FY": 30})


@pytest.fixture
def r(tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path)
    sid = store.save(source="sec_companyfacts", payload=_cf_doc(AAPL_ENDS), url="u", fetched_at=datetime.now())
    led = FactLedger()
    for p, v in (("2024FY", 391), ("2025FY", 416)):
        led.put(_f("revenue", p, v, AAPL_ENDS[p] + __import__("datetime").timedelta(days=34)))
    peers = Peers(target="AAPL", target_name="Apple Inc.",
                  classification=UsClassification(code="AAPL", name="Apple Inc.", path="3571",
                                                  used_path="FF49-35 Hardw 计算机", level=1),
                  members=[Member("DELL", "Dell"), Member("HPQ", "Hp"), Member("SMCI", "Smci"),
                           Member("IFRS", "外国发行人")])
    monkeypatch.setattr(us_industry, "peer_set_us", lambda code, st: peers)

    def books(members, st, as_of, notes):
        notes.append("IFRS 外国发行人：没有 us-gaap 事实（多为只报 IFRS 的外国发行人），剔除")
        return {"DELL": DELL, "HPQ": HPQ, "SMCI": SMCI}
    monkeypatch.setattr(us_peers, "load_peer_books", books)
    q = lambda p: Quote(price=D(p), as_of=date(2026, 10, 23), source="tiingo_iex", source_id="iex")  # noqa: E731
    monkeypatch.setattr(us_quotes, "fetch_peer_quotes",
                        lambda ts, st: ({"DELL": q("120"), "HPQ": q("30"), "SMCI": q("40")}, []))
    return SimpleNamespace(ledger=led, store=store, period="2025FY", as_of=AS_OF,
                           sec_meta={"companyfacts_sid": sid, "fye": "0927"})


def test_美股行业聚合写入账本且同业数据交给后续步骤(r):
    human, model, err = full._load_industry_us("AAPL", r, AS_OF)
    assert err == ""
    assert r.ledger.get("industry.peer_count", "2025FY", as_of=AS_OF).value == 4   # 本司 + 3 家
    assert set(r.peer_books) == {"DELL", "HPQ", "SMCI"} and set(r.peer_quotes) == {"DELL", "HPQ", "SMCI"}
    assert all(p.endswith("FY") for c in r.peer_statements.values() for _, p in c)
    assert r.target_ends["2025FY"] == date(2025, 9, 27)
    assert r.peer_set.target == "AAPL"
    assert any("IFRS" in n for n in r.peer_notes)
    assert "FF49-35" in human and "[[industry." in model


def test_同业按期末最近的财年计入本司FY2025营收(r):
    full._load_industry_us("AAPL", r, AS_OF)
    # 苹果 FY2025 末 2025-09-27：戴尔取 FY2026（2026-01-30），惠普取 FY2025（2025-10-31），
    # 超微取 FY2026（2026-06-30，相距 276 天 > 半年 → 不对齐；FY2025 2025-06-30 相距 89 天）
    tot = r.ledger.get("industry.revenue", "2025FY", as_of=AS_OF)
    assert tot.value == D(416 + 100 + 55 + 22) and tot.unit == "美元"


def test_行情不可用时聚合照做_说明写清(r, monkeypatch):
    monkeypatch.setattr(us_quotes, "fetch_peer_quotes",
                        lambda ts, st: (_ for _ in ()).throw(QuoteUnavailable("行情不可用：未设置 TIINGO_API_KEY")))
    _human, _model, err = full._load_industry_us("AAPL", r, AS_OF)
    assert r.peer_quotes == {} and "行情" in err and "TIINGO_API_KEY" in err
    assert r.ledger.get("industry.peer_count", "2025FY", as_of=AS_OF).value == 4


def test_A股行业总营收单位仍是元():
    from ir_agent.industry_facts import IndustryAggregate, industry_facts
    agg = IndustryAggregate(target="600183", target_name="生益科技", industry="电子", period="2025FY",
                            prior_period="2024FY", as_of=AS_OF, thin=False)
    agg.names = {"600183": "a", "1": "b", "2": "c"}
    agg.revenues = {"600183": D(10), "1": D(20), "2": D(30)}
    facts = {f.key: f for f in industry_facts(agg)}
    if "industry.revenue" in facts:
        assert facts["industry.revenue"].unit == "元"
