from datetime import date
from decimal import Decimal as D

import pytest

from ir_agent import full, methodology
from ir_agent.sources import edgar, us_industry
from ir_agent.sources.external import Tier
from ir_agent.sources.openweb import tier_of_url


@pytest.fixture
def fake_sec(monkeypatch):
    info = edgar.TickerInfo(cik="0000320193", name="Apple Inc.", exchange="Nasdaq")
    monkeypatch.setattr(edgar, "resolve_ticker", lambda t, store: info)
    monkeypatch.setattr(edgar, "fetch_submissions", lambda cik, store: ({"sic": "3571"}, "sid"))
    full._us_meta.cache_clear()


def test_美股公司名取SEC(fake_sec):
    assert full._company_name("AAPL") == "Apple Inc."


def test_美股行业路径取FF49且不枚举同业(fake_sec, monkeypatch):
    monkeypatch.setattr(us_industry, "peer_set_us", lambda *a, **k: (_ for _ in ()).throw(AssertionError("同业")))
    assert full._industry_path("AAPL") == "FF49-35 Hardw 计算机"


def test_美股同业名取FF49同业(fake_sec, monkeypatch):
    from ir_agent.sources.industry import Member, Peers
    ps = Peers(target="AAPL", target_name="Apple Inc.", classification=None,
               members=[Member("DELL", "Dell Technologies Inc."), Member("HPQ", "HP INC")])
    monkeypatch.setattr(us_industry, "peer_set_us", lambda t, store: ps)
    assert full._peer_names("AAPL") == ("Apple Inc.", "Dell Technologies Inc.", "HP INC")


def test_方法论预研提示词按市场():
    us = methodology.build_prompt("FF49-35 Hardw 计算机", "AAPL")
    cn = methodology.build_prompt("电子-半导体", "600584")
    assert "美股公司研究报告" in us and "A 股" not in us.split("\n")[0]
    assert "A 股公司研究报告" in cn


@pytest.mark.parametrize("url,tier", [("https://www.bls.gov/cpi/", Tier.OFFICIAL),
                                     ("https://www.census.gov/retail/", Tier.OFFICIAL),
                                     ("https://bls.gov.evil.com/x", Tier.MEDIA),
                                     ("https://notgov.com/x", Tier.MEDIA),
                                     ("https://www.stats.gov.cn/x", Tier.OFFICIAL)])
def test_美国政府域名判为官方(url, tier):
    assert tier_of_url(url) is tier


def test_产业链规模口径按市场():
    from ir_agent.value_chain import ChainView
    v = ChainView(market_scope="同一 Fama-French 行业的美国上市公司合计")
    assert "美国上市公司合计" in v.summary() and "A 股" not in v.summary()
    assert "A 股同行业上市公司合计" in ChainView().summary()


def test_只报合并SGA时DOL可算():
    from ir_agent import industry_profile as ip
    from ir_agent.ledger import Fact, FactLedger, Method
    led = FactLedger()
    for k, v in (("operating_profit", 100), ("sga_expense", 50)):
        led.put(Fact(key=k, value=D(v), unit="美元", currency="USD", period="2025FY",
                     as_of=date(2026, 1, 1), source_id="s", method=Method.REPORTED))
    assert ip.dol(led, "2025FY", date(2026, 6, 1)) == D("1.5")
