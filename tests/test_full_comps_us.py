"""美股可比：按 IEX 市值就近选六家，可比表与 A 股同一套指标；PE 分母用按日期对齐的同业 TTM。不联网。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from ir_agent import full
from ir_agent.comparables import Sized, nearest, select
from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.periods_spot import spot_period
from ir_agent.relval_data import flow_ttm
from ir_agent.sources.industry import Member, Peers
from ir_agent.sources.us_industry import UsClassification
from ir_agent.sources.us_peers import PeerBook
from ir_agent.sources.us_quotes import Quote

AS_OF = date(2026, 10, 26)
QDAY = date(2026, 10, 23)
SPOT = spot_period(QDAY)
AAPL_ENDS = {"2024FY": date(2024, 9, 28), "2025FY": date(2025, 9, 27), "2025Q1-Q3": date(2025, 6, 28),
             "2026Q1-Q3": date(2026, 6, 27)}


def _f(key, period, v, as_of, unit="美元"):
    return Fact(key=key, value=D(str(v)), unit=unit, currency="USD" if unit == "美元" else None,
                period=period, as_of=as_of, source_id="cf", method=Method.REPORTED)


def _book(t, shares, np_fy=10, quarterly=None):
    """九月财年（与苹果同历）的同业；quarterly={标签: 归母净利润累计} 给出季报。"""
    ends = dict(AAPL_ENDS)
    fs = [_f("shares_outstanding", "2026-10-01", shares, date(2026, 8, 1), unit="股")]
    for p in ("2024FY", "2025FY"):
        filed = ends[p] + timedelta(days=40)
        fs += [_f("revenue", p, 100, filed), _f("net_profit_attr_parent", p, np_fy, filed),
               _f("equity_attr_parent", p, 50, filed), _f("total_assets", p, 120, filed)]
    for p, v in (quarterly or {}).items():
        fs.append(_f("net_profit_attr_parent", p, v, ends[p] + timedelta(days=40)))
    return PeerBook(ticker=t, name=t.title(), cik="1", fye="0927", facts=fs, ends=ends)


CAPS = {"P1": 900, "P2": 700, "P3": 500, "P4": 300, "P5": 200, "P6": 150, "P7": 90, "P8": 40}


@pytest.fixture
def r(tmp_path):
    from ir_agent.us_align import calendarize
    books = {t: _book(t, shares=c, quarterly={"2025Q1-Q3": 7, "2026Q1-Q3": 9}) for t, c in CAPS.items()}
    books["NOCAP"] = _book("NOCAP", shares=1)
    books["NOCAP"].facts = [f for f in books["NOCAP"].facts if f.key != "shares_outstanding"]
    q = Quote(price=D(1), as_of=QDAY, source="tiingo_iex", source_id="iex")
    led = FactLedger()
    for f in _book("AAPL", 400, quarterly={"2025Q1-Q3": 70, "2026Q1-Q3": 90}).facts:
        if f.key != "shares_outstanding":
            led.put(f)
    led.put(_f("market_cap", SPOT, 400, QDAY))
    led.put(_f("last_price", SPOT, 1, QDAY))
    led.put(_f("net_profit_ttm", "2026Q1-Q3", 30, date(2026, 8, 6)))
    peers = Peers(target="AAPL", target_name="Apple Inc.",
                  classification=UsClassification(code="AAPL", name="Apple Inc.", path="3571",
                                                  used_path="FF49-35 Hardw 计算机", level=1),
                  members=[Member(t, t.title()) for t in books])
    return SimpleNamespace(
        ledger=led, period="2025FY", as_of=AS_OF, peer_set=peers, peer_books=books,
        peer_statements={t: calendarize(b, AAPL_ENDS, AS_OF) for t, b in books.items()},
        peer_quotes={t: q for t in books}, peer_notes=[], target_ends=AAPL_ENDS,
        quotes=SimpleNamespace(facts=[_f("market_cap", SPOT, 400, QDAY)]))


def test_nearest与原select同序(monkeypatch):
    sized = [Sized(c, c, D(v)) for c, v in {"T": 300, **CAPS}.items()]
    chosen, me = nearest(sized, "T", 6)
    from ir_agent import comparables
    monkeypatch.setattr(comparables, "fetch_caps", lambda codes, session=None: (sized, []))
    peers = SimpleNamespace(target="T", codes=list(CAPS), members=list(CAPS))
    assert [s.code for s in select(peers, 6).chosen] == [s.code for s in chosen]
    assert me.code == "T" and len(chosen) == 6


def test_美股可比按IEX市值就近六家且无市值者不进(r):
    human, prompt, err = full._load_comps("AAPL", r, AS_OF, None)
    assert err == ""
    assert r.comps_codes == ["P1", "P2", "P3", "P4", "P5", "P6"]          # 400 居中，两侧就近
    assert "NOCAP" not in r.comps_codes
    assert "可比公司 6 家" in human and "亿美元" in human and "NOCAP" in human   # 剔除原因留在摘要里


def test_美股可比表PE用对齐后的TTM(r):
    full._load_comps("AAPL", r, AS_OF, None)
    # 同业 TTM = 2026Q1-Q3 累计 9 + 2025FY 10 − 2025Q1-Q3 7 = 12；市值 = 股数 × 1
    row_led = full._us_comps_ledger(r, "P3", SPOT)
    assert row_led.get("net_profit_ttm", "2026Q1-Q3", as_of=AS_OF).value == D(12)
    assert row_led.get("market_cap", SPOT, as_of=AS_OF).value == D(500)


def test_行情日与本司不同的同业不进可比(r):
    r.peer_quotes["P3"] = Quote(price=D(1), as_of=date(2026, 10, 22), source="tiingo_iex", source_id="iex")
    human, _p, _e = full._load_comps("AAPL", r, AS_OF, None)
    assert "P3" not in r.comps_codes
    assert "P3" in human and "2026-10-22" in human


def test_没有同业数据时说明原因_不碰A股():
    r = SimpleNamespace(ledger=FactLedger(), period="2025FY", quotes=None)
    human, prompt, err = full._load_comps("AAPL", r, AS_OF, None)
    assert (human, prompt) == ("", "") and "行业同业未取到" in err


def test_美股账本季报截止的流量TTM():
    led = FactLedger()
    for p, v, d in (("2026Q1-Q3", 9, date(2026, 8, 6)), ("2025FY", 10, date(2025, 11, 6)),
                    ("2025Q1-Q3", 7, date(2025, 8, 6))):
        led.put(_f("operating_profit", p, v, d))
    assert flow_ttm(led, "operating_profit", (2026, 3), AS_OF) == D(12)


def test_A股流量TTM仍只认中报年报():
    led = FactLedger()
    for p in ("2026Q1-Q3", "2025FY", "2025Q1-Q3"):
        led.put(Fact(key="operating_profit", value=D(1), unit="元", currency="CNY", period=p,
                     as_of=date(2026, 10, 1), source_id="em", method=Method.REPORTED))
    assert flow_ttm(led, "operating_profit", (2026, 3), AS_OF) is None
