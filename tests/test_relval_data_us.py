"""美股相对估值取数 gather_us（计划 Task 7）：同业按期末日期对齐本司，NTM 只给本司与可比。不联网。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.periods_spot import spot_period
from ir_agent.relval_data import RelvalUnavailable
from ir_agent.sources.industry import Member, Peers
from ir_agent.sources.reports import Consensus, Estimate
from ir_agent.sources.us_industry import UsClassification
from ir_agent.sources.us_peers import PeerBook
from ir_agent.sources.us_quotes import Quote

AS_OF = date(2026, 10, 26)
QDAY = date(2026, 10, 23)
SPOT = spot_period(QDAY)
AAPL_ENDS = {"2024FY": date(2024, 9, 28), "2025FY": date(2025, 9, 27), "2025Q1-Q3": date(2025, 6, 28),
             "2026Q1-Q3": date(2026, 6, 27)}
DELL_ENDS = {"2025FY": date(2025, 1, 31), "2026FY": date(2026, 1, 30), "2026H1": date(2025, 8, 1),
             "2027H1": date(2026, 7, 31), "2027Q1": date(2026, 5, 1)}


def _f(key, period, v, as_of, unit="美元"):
    return Fact(key=key, value=D(str(v)), unit=unit, currency="USD" if unit == "美元" else None,
                period=period, as_of=as_of, source_id="cf", method=Method.REPORTED)


def _facts(ends, fy_labels, cum: dict[str, float], lag=40):
    fs = []
    for p in fy_labels:
        d = ends[p] + timedelta(days=lag)
        fs += [_f(k, p, v, d) for k, v in (("revenue", 400), ("net_profit_attr_parent", 100),
                                           ("operating_profit", 120), ("depreciation_fixed_asset", 10),
                                           ("equity_attr_parent", 70), ("total_equity", 70),
                                           ("cash_and_equivalents", 30))]
    for p, f in cum.items():
        d = ends[p] + timedelta(days=lag)
        fs += [_f(k, p, v * f, d) for k, v in (("revenue", 400), ("net_profit_attr_parent", 100),
                                               ("operating_profit", 120), ("depreciation_fixed_asset", 10))]
        fs += [_f(k, p, v, d) for k, v in (("equity_attr_parent", 70), ("total_equity", 70),
                                          ("cash_and_equivalents", 30))]
    return fs


def _book(t, ends, fy, cum, fye="0927", shares=10):
    fs = _facts(ends, fy, cum) + [_f("shares_outstanding", "2026-10-01", shares, date(2026, 8, 1), unit="股")]
    return PeerBook(ticker=t, name=t.title(), cik="1", fye=fye, facts=fs, ends=dict(ends))


@pytest.fixture
def r():
    led = FactLedger()
    for f in _facts(AAPL_ENDS, ("2024FY", "2025FY"), {"2025Q1-Q3": 0.7, "2026Q1-Q3": 0.8}):
        led.put(f)
    led.put(_f("net_profit_ttm", "2026Q1-Q3", 110, date(2026, 8, 7)))
    led.put(_f("market_cap", SPOT, 4000, QDAY))
    led.put(_f("last_price", SPOT, 40, QDAY))
    books = {f"P{i}": _book(f"P{i}", AAPL_ENDS, ("2024FY", "2025FY"), {"2025Q1-Q3": 0.7, "2026Q1-Q3": 0.8},
                             shares=10 + i) for i in range(5)}
    books["DELL"] = _book("DELL", DELL_ENDS, ("2025FY", "2026FY"), {"2026H1": 0.5, "2027H1": 0.55},
                          fye="0131", shares=20)
    books["OLDFY"] = _book("OLDFY", {"2024FY": date(2024, 1, 31)}, ("2024FY",), {}, fye="0131")
    books["NOCAP"] = _book("NOCAP", AAPL_ENDS, ("2025FY",), {})
    books["NOCAP"].facts = [f for f in books["NOCAP"].facts if f.key != "shares_outstanding"]
    q = lambda d=QDAY: Quote(price=D(40), as_of=d, source="tiingo_iex", source_id="iex")  # noqa: E731
    quotes = {t: q() for t in books}
    quotes["STALE"] = q(date(2026, 10, 22))
    books["STALE"] = _book("STALE", AAPL_ENDS, ("2025FY",), {})
    peers = Peers(target="AAPL", target_name="Apple Inc.",
                  classification=UsClassification(code="AAPL", name="Apple Inc.", path="3571",
                                                   used_path="FF49-35 Hardw 计算机", level=1),
                  members=[Member(t, t) for t in books])
    return SimpleNamespace(code="AAPL", ledger=led, period="2025FY", as_of=AS_OF, peer_set=peers,
                           peer_books=books, peer_quotes=quotes, peer_notes=["IFRS 某：剔除"],
                           target_ends=AAPL_ENDS, comps_codes=["P1", "P2", "DELL"], store=None,
                           quotes=SimpleNamespace(facts=[_f("market_cap", SPOT, 4000, QDAY)]),
                           sec_meta={"fye": "0927"})


@pytest.fixture
def cons(monkeypatch):
    from ir_agent.sources import fmp
    asked = []

    def fake(ticker, fye, as_of, store, today=None):
        asked.append(ticker)
        return Consensus(as_of=as_of, window_days=0, stat="均值", source_id=f"fmp_{ticker}",
                         eps={y: Estimate(fiscal_year=y, n=20, median=D(v), low=D(v), high=D(v))
                              for y, v in ((2027, "9"), (2028, "10"))})
    monkeypatch.setattr(fmp, "fetch_consensus", fake)
    return asked


def test_NTM按本财年剩余天数加权():
    from ir_agent.relval_data_us import ntm_eps
    c = Consensus(as_of=AS_OF, window_days=0, eps={y: Estimate(fiscal_year=y, n=20, median=D(v), low=D(v), high=D(v))
                                                   for y, v in ((2027, "9"), (2028, "10"))})
    v, basis = ntm_eps(c, "0927", AS_OF)
    assert abs(v - (D(10) - D(336) / D(365))) < D("1e-20") and "FY2027" in basis and "FY2028" in basis


def test_NTM缺下一财年预期给None():
    from ir_agent.relval_data_us import ntm_eps
    c = Consensus(as_of=AS_OF, window_days=0, eps={2027: Estimate(fiscal_year=2027, n=20, median=D(9),
                                                                   low=D(9), high=D(9))})
    assert ntm_eps(c, "0927", AS_OF) is None


def test_本司观测_利润口径按日期写_现价与NTM(r, cons):
    from ir_agent.relval_data_us import gather_us
    tgt, sample, comps, shares, price, notes = gather_us(r, AS_OF, None)
    assert tgt.np_basis == "TTM 截至 2026-06-27" and tgt.net_profit == D(110)
    assert tgt.price == D(40) and abs(tgt.eps_ntm - (D(10) - D(336) / D(365))) < D("1e-20")
    assert shares == D(100) and price == D(40)


def test_苹果对戴尔_利润TTM取同一季而非同名标签(r, cons):
    from ir_agent.relval_data_us import gather_us
    _t, sample, *_ = gather_us(r, AS_OF, None)
    dell = next(o for o in sample if o.code == "DELL")
    assert dell.np_basis == "TTM 截至 2026-07-31" and dell.lags == {}
    # TTM = 2027H1 累计 55 + FY2026 100 − 2026H1 累计 50
    assert abs(dell.net_profit - D(105)) < D("1e-9")       # 夹具用浮点乘出累计数


def test_剔除逐家说明_行情日不同写明日期_无对齐年报_无市值(r, cons):
    from ir_agent.relval_data_us import gather_us
    _t, sample, _c, _s, _p, notes = gather_us(r, AS_OF, None)
    codes = {o.code for o in sample}
    assert {"STALE", "OLDFY", "NOCAP"}.isdisjoint(codes) and {"P0", "DELL"} <= codes
    text = "\n".join(notes)
    assert "STALE" in text and "2026-10-22" in text
    assert any(n.startswith("OLDFY") and "对齐" in n for n in notes)
    assert any(n.startswith("NOCAP") and "封面" in n for n in notes)
    assert "IFRS 某：剔除" in notes


def test_一致预期只请求本司与可比(r, cons):
    from ir_agent.relval_data_us import gather_us
    gather_us(r, AS_OF, None)
    assert set(cons) == {"AAPL", "P1", "P2", "DELL"}


def test_可比与回归样本是同一份观测(r, cons):
    from ir_agent.relval_data_us import gather_us
    _t, sample, comps, *_ = gather_us(r, AS_OF, None)
    assert [c.code for c in comps] == ["P1", "P2", "DELL"]
    assert all(any(c is s for s in sample) for c in comps)
    assert all(c.eps_ntm is not None for c in comps)
    assert all(s.eps_ntm is None for s in sample if s.code not in ("P1", "P2", "DELL"))


def test_没有同业数据时明确不可用(r, cons):
    from ir_agent.relval_data_us import gather_us
    r.peer_books = {}
    with pytest.raises(RelvalUnavailable):
        gather_us(r, AS_OF, None)
