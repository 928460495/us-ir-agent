from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

from ir_agent import full
from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.sources import fred, macro, us_industry
from ir_agent.sources.industry import Classification, Member, Peers

AS_OF = date(2025, 11, 15)


def _f(key, period, v, unit="ratio"):
    return Fact(key=key, value=D(v), unit=unit, period=period, as_of=date(2025, 11, 1),
                source_id="fred_x", method=Method.REPORTED)


def _run(code="AAPL", period="2025FY"):
    return SimpleNamespace(code=code, ledger=FactLedger(), store=None, period=period)


def test_美股宏观取FRED不取中国宏观(monkeypatch):
    monkeypatch.setattr(macro, "fetch_macro", lambda: (_ for _ in ()).throw(AssertionError("A 股宏观")))
    monkeypatch.setattr(fred, "fetch_us_macro", lambda as_of, store: (
        [_f("macro.term_spread", "2025-11-12", "0.0055"), _f("macro.cpi_yoy", "2025-09", "0.03")],
        ["FRED UMCSENT HTTP 500"]))
    r = _run()
    human, prompt, err = full._load_macro(r, AS_OF)
    assert r.ledger.get("macro.cpi_yoy", "2025-09", as_of=AS_OF).value == D("0.03")
    assert "期限利差" in human and "[领先]" in human and "0.55%" in human
    assert "[[macro.term_spread@2025-11-12]]" in prompt and "0.55" not in prompt
    assert "UMCSENT" in err


def test_A股宏观路径不变(monkeypatch):
    monkeypatch.setattr(macro, "fetch_macro", lambda: [_f("macro.cpi_yoy", "2025-09", "0.01")])
    monkeypatch.setattr(fred, "fetch_us_macro", lambda *a: (_ for _ in ()).throw(AssertionError("FRED")))
    human, _, _ = full._load_macro(_run("600183"), AS_OF)
    assert "CPI" in human


def test_美股无风险利率取美债十年且β留待阶段3(monkeypatch):
    monkeypatch.setattr(fred, "fetch_series_asof", lambda sid, as_of, store: [
        _f("rate.ust_10y", "2025-11-12", "0.0412"), _f("rate.ust_10y", "2025-11-13", "0.0408")])
    r = _run()
    human, prompt, err = full._load_capm("AAPL", r, AS_OF)
    assert "4.08%" in human and "美国十年期国债" in human
    assert "[[rate.ust_10y@2025-11-13]]" in prompt
    assert "β" in err and "阶段 3" in err


def test_美股行业给出FF49同业名单且聚合留待后续阶段(monkeypatch):
    cls = us_industry.UsClassification(code="AAPL", name="Apple Inc.", path="3571",
                                       used_path="FF49-35 Hardw 计算机", level=1)
    peers = Peers(target="AAPL", target_name="Apple Inc.", classification=cls,
                  members=[Member("DELL", "Dell"), Member("HPQ", "HP")], thin=True)
    monkeypatch.setattr(us_industry, "peer_set_us", lambda t, store: peers)
    human, prompt, err = full._load_industry("AAPL", _run(), AS_OF)
    assert "FF49-35" in human and "DELL" in human
    assert "行业聚合" in err and "阶段" in err
