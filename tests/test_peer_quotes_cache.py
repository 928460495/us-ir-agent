"""同业行情按分析时点落盘、后续阶段读回（用户 2026-11-17 选方案 C）。不联网。

同业行情只有当天价（Tiingo IEX 批量）；第②–④步隔天运行时报价日与本司分析时点收盘对不上，可比与相对估值
整节缺位。第一次取到的行情按分析时点存索引，同一分析时点的后续阶段直接读回，不再联网。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from ir_agent.sources import us_quotes
from ir_agent.sources.snapshot import SnapshotStore

AS_OF = date(2026, 11, 15)


def _fake(monkeypatch, day):
    calls = []

    def fetch(tickers, store):
        calls.append(list(tickers))
        return {t: us_quotes.Quote(price=D("10"), as_of=day, source="tiingo_iex", source_id="sid")
                for t in tickers}, [f"{t}：x" for t in tickers[:1]]
    monkeypatch.setattr(us_quotes, "fetch_peer_quotes", fetch)
    return calls


def test_第一次联网取并落索引_之后读回不联网(monkeypatch, tmp_path):
    store = SnapshotStore(tmp_path)
    calls = _fake(monkeypatch, date(2026, 11, 13))
    q1, n1 = us_quotes.fetch_peer_quotes_cached(["AAA", "BBB"], store, AS_OF)
    assert len(calls) == 1 and q1["AAA"].as_of == date(2026, 11, 13)
    calls2 = _fake(monkeypatch, date(2026, 11, 17))            # 隔天再取会是 11-17 的价
    q2, n2 = us_quotes.fetch_peer_quotes_cached(["AAA", "BBB"], store, AS_OF)
    assert calls2 == [] and q2["AAA"].as_of == date(2026, 11, 13) and q2["BBB"].price == D("10")
    assert any("读回" in x for x in n2)


def test_不同分析时点各存各的(monkeypatch, tmp_path):
    store = SnapshotStore(tmp_path)
    _fake(monkeypatch, date(2026, 11, 13))
    us_quotes.fetch_peer_quotes_cached(["AAA"], store, AS_OF)
    calls = _fake(monkeypatch, date(2026, 11, 20))
    q, _ = us_quotes.fetch_peer_quotes_cached(["AAA"], store, date(2026, 11, 20))
    assert len(calls) == 1 and q["AAA"].as_of == date(2026, 11, 20)


def test_名单多出新同业时补取缺的那几只(monkeypatch, tmp_path):
    store = SnapshotStore(tmp_path)
    _fake(monkeypatch, date(2026, 11, 13))
    us_quotes.fetch_peer_quotes_cached(["AAA"], store, AS_OF)
    calls = _fake(monkeypatch, date(2026, 11, 13))
    q, _ = us_quotes.fetch_peer_quotes_cached(["AAA", "CCC"], store, AS_OF)
    assert calls == [["CCC"]] and set(q) == {"AAA", "CCC"}


def test_行业加载走缓存版本():
    import inspect
    from ir_agent import full
    assert "fetch_peer_quotes_cached(" in inspect.getsource(full._load_industry_us)
