"""美股第一次调用（--analyse）不碰任何 A 股数据源。"""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from ir_agent import full
from ir_agent.ledger import FactLedger
from ir_agent.sources.industry import IndustryUnavailable
from ir_agent.sources.snapshot import SnapshotStore

AS_OF = date(2026, 10, 25)


@pytest.fixture
def no_cn(monkeypatch):
    """A 股数据源一律打桩成抛错：被调用即测试失败。"""
    def boom(name):
        def f(*a, **k):
            raise AssertionError(f"美股路径调用了 A 股数据源：{name}")
        return f
    from ir_agent import beta, corpus
    from ir_agent.sources import announce, eastmoney, industry, macro, rates, reports, taxonomy
    for mod, attr in ((eastmoney, "fetch_statements_em"), (macro, "fetch_macro"),
                      (rates, "fetch_yields"), (beta, "estimate"), (industry, "peer_set"),
                      (taxonomy, "fetch_all"), (reports, "search"), (corpus, "gather"),
                      (announce, "gather")):
        monkeypatch.setattr(mod, attr, boom(f"{mod.__name__}.{attr}"))


@pytest.fixture
def us_sources(monkeypatch):
    from ir_agent.sources import fmp, fomc, fred, us_industry
    monkeypatch.setattr(fred, "fetch_us_macro", lambda as_of, store: ([], []))
    monkeypatch.setattr(fred, "fetch_series_asof", lambda sid, as_of, store: [])
    monkeypatch.setattr(us_industry, "peer_set_us",
                        lambda t, store: (_ for _ in ()).throw(IndustryUnavailable("桩")))
    monkeypatch.setattr(fmp, "fetch_consensus", lambda *a, **k: None)
    monkeypatch.setattr(fomc, "fetch_statements", lambda as_of, store: ([], ""))


def _r(tmp_path):
    st = SnapshotStore(tmp_path)
    sid = st.save(source="sec_submissions", payload={"cik": "320193", "fiscalYearEnd": "0927"},
                  url="u", fetched_at=datetime.now())
    return SimpleNamespace(code="AAPL", ledger=FactLedger(), store=st, period="2025FY", corpus=None,
                           consensus=None, quotes=None, sec_meta={"submissions_sid": sid})


def test_美股可比在行业同业缺失时说明原因且不碰东财(no_cn, tmp_path):
    human, prompt, err = full._load_comps("AAPL", _r(tmp_path), AS_OF, tmp_path)
    assert (human, prompt) == ("", "") and "行业同业未取到" in err


def test_美股确定性分析层不调用A股数据源(no_cn, us_sources, tmp_path):
    md, pr, skipped = full._analyse_facts("AAPL", _r(tmp_path), AS_OF, tmp_path)
    assert not any("A 股数据源" in s for s in skipped), skipped
    assert set(md) == {"宏观", "折现率输入", "商业周期", "行业", "可比", "外部资料"}
