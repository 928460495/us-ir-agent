from datetime import date
from decimal import Decimal as D

import pytest

from ir_agent.periods import sort_key
from ir_agent.sources import fred
from ir_agent.sources.macro import MacroUnavailable
from ir_agent.sources.snapshot import SnapshotStore

KEY = "fredsecret"


def _obs(rt, d, v, rt_end="9999-12-31"):
    return {"realtime_start": rt, "realtime_end": rt_end, "date": d, "value": v}


def _vintage(series):
    """完整的版本史（ALFRED 原貌）。GDPC1 2024Q4 初值同比 2.8%，2025-03-27 修订为 3.1%。"""
    if series == "GDPC1":
        return [_obs("2024-10-30", "2023-10-01", "100.0"),
                _obs("2025-01-30", "2024-10-01", "102.8", "2025-03-26"),
                _obs("2025-03-27", "2024-10-01", "103.1")]
    if series == "DGS10":
        return [_obs("2025-10-22", "2025-10-21", "4.10"), _obs("2025-10-24", "2025-10-23", "4.12"),
                _obs("2025-10-25", "2025-10-24", ".")]
    if series == "CPIAUCSL":
        return [_obs("2024-10-10", "2024-09-01", "314.0"), _obs("2025-10-15", "2025-09-01", "323.42")]
    if series == "GDP":
        return [_obs("2024-10-30", "2023-10-01", "28000.0"), _obs("2025-01-30", "2024-10-01", "29700.0")]
    return []


def _fred_clip(rows, rs, re_):
    """FRED 的真实行为（审查 I5）：只返回与请求窗口相交的版本，且把每条的
    realtime_start / realtime_end 截到窗口内。"""
    out = []
    for o in rows:
        if o["realtime_start"] > re_ or o["realtime_end"] < rs:
            continue
        out.append({**o, "realtime_start": max(o["realtime_start"], rs),
                    "realtime_end": min(o["realtime_end"], re_)})
    return out


@pytest.fixture
def fake_fred(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    seen = []

    class R:
        status_code = 200

        def __init__(self, body):
            self._b = body

        def json(self):
            return self._b

    def get(url, params=None, timeout=None):
        seen.append(dict(params))
        rows = _fred_clip(_vintage(params["series_id"]), params["realtime_start"], params["realtime_end"])
        return R({"observations": rows})
    monkeypatch.setattr(fred.requests, "get", get)
    return seen


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path)


def get(facts, key, period=None):
    cands = [f for f in facts if f.key == key and (period is None or f.period == period)]
    return max(cands, key=lambda f: sort_key(f.period))


def test_取当时可见的初值而非修订值(fake_fred, store):
    early = fred.fetch_series_asof("GDPC1", date(2025, 2, 1), store)
    late = fred.fetch_series_asof("GDPC1", date(2025, 4, 1), store)
    assert get(early, "macro.gdp_real_yoy").value == D("0.028")
    assert get(late, "macro.gdp_real_yoy").value == D("0.031")


def test_版本窗口与观测窗口同起点_止于as_of(fake_fred, store):
    # 从 1776 年开窗时日度序列有五千多个版本，超过 FRED 单次两千个的上限（联网验收：DGS10 HTTP 400）
    fred.fetch_series_asof("GDPC1", date(2025, 2, 1), store)
    assert fake_fred[0]["realtime_start"] == fake_fred[0]["observation_start"]
    assert fake_fred[0]["realtime_end"] == "2025-02-01"


def test_事实as_of取该版本的真实生效日而非请求日(fake_fred, store):
    f = get(fred.fetch_series_asof("GDPC1", date(2025, 4, 1), store), "macro.gdp_real_yoy")
    assert f.as_of == date(2025, 3, 27) and f.period == "2024Q4S"


def test_国债收益率转为小数(fake_fred, store):
    f = get(fred.fetch_series_asof("DGS10", date(2025, 10, 25), store), "rate.ust_10y")
    assert f.value == D("0.0412") and f.unit == "ratio" and f.period == "2025-10-23"


def test_缺值点号跳过(fake_fred, store):
    fs = fred.fetch_series_asof("DGS10", date(2025, 10, 25), store)
    assert "2025-10-24" not in {f.period for f in fs}


def test_月度同比与标签(fake_fred, store):
    f = get(fred.fetch_series_asof("CPIAUCSL", date(2025, 10, 25), store), "macro.cpi_yoy")
    assert f.period == "2025-09" and f.value == D("323.42") / D("314.0") - 1


def test_一个序列产出多个键(fake_fred, store):
    fs = fred.fetch_series_asof("GDP", date(2025, 10, 25), store)
    assert get(fs, "macro.gdp_nominal").value == D("29700.0") * D(10) ** 9
    assert get(fs, "macro.gdp_nominal").unit == "美元"
    assert get(fs, "macro.gdp_nominal_yoy").value == D("29700.0") / D("28000.0") - 1


def test_缺key时宏观不可用(monkeypatch, store):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(MacroUnavailable, match="FRED_API_KEY"):
        fred.fetch_series_asof("CPIAUCSL", date(2025, 10, 25), store)


def test_key不落快照(fake_fred, tmp_path):
    fred.fetch_series_asof("DGS10", date(2025, 10, 25), SnapshotStore(tmp_path))
    assert all(KEY not in p.read_text(encoding="utf-8") for p in tmp_path.iterdir())


def test_登记表有分类且不含A股专有指标():
    keys = {s.key for s in fred.US_MACRO}
    assert {"macro.cpi_yoy", "macro.term_spread", "rate.ust_10y"} <= keys
    assert not any(k.startswith(("macro.pmi", "macro.m1", "macro.m2")) for k in keys)
    assert fred.classify("macro.term_spread") == "领先"
    assert fred.classify("macro.cpi_yoy") == "滞后"
    assert fred.classify("rate.ust_10y") is None


def test_整批取数时单个序列失败不拖垮其余(fake_fred, store, monkeypatch):
    orig = fred.fetch_series_asof

    def flaky(sid, as_of, store):
        if sid == "UMCSENT":
            raise MacroUnavailable("FRED UMCSENT HTTP 500")
        return orig(sid, as_of, store)
    monkeypatch.setattr(fred, "fetch_series_asof", flaky)
    facts, notes = fred.fetch_us_macro(date(2025, 10, 25), store)
    assert any(f.key == "rate.ust_10y" for f in facts)
    assert any("UMCSENT" in n for n in notes)


def test_登记表有美元指数():
    reg = {s.key: s for s in fred.US_MACRO}
    lvl, yoy = reg["macro.usd_index"], reg["macro.usd_index_yoy"]
    assert lvl.series_id == yoy.series_id == "TWEXBGSMTH"
    assert (lvl.transform, lvl.freq, lvl.unit) == ("level", "M", "index")
    assert (yoy.transform, yoy.freq) == ("yoy", "M")
    assert fred.classify("macro.usd_index") == fred.classify("macro.usd_index_yoy") == "汇率"


def test_美元指数进宏观摘要与提示词():
    from ir_agent.citation import audit_bare_numbers
    from ir_agent.ledger import Fact, Method
    fs = [Fact(key=k, value=D(v), unit=u, period="2026-09", as_of=date(2026, 10, 1),
               source_id="fred", method=Method.REPORTED)
          for k, v, u in (("macro.usd_index", "121.5", "index"), ("macro.usd_index_yoy", "0.031", "ratio"),
                          ("macro.cpi_yoy", "0.03", "ratio"))]
    snap = fred.UsMacroSnapshot(fs, as_of=date(2026, 10, 25))
    assert "[汇率] 广义美元指数（2026-09）121.5" in snap.describe()
    p = snap.for_prompt()
    assert "[汇率] 广义美元指数 [[macro.usd_index@2026-09]]　广义美元指数同比 [[macro.usd_index_yoy@2026-09]]" in p
    assert audit_bare_numbers(p) == []


def test_A股宏观提示词分组不变():
    from ir_agent.ledger import Fact, Method
    from ir_agent.sources.macro import MacroSnapshot
    f = Fact(key="macro.cpi_yoy", value=D("0.01"), unit="ratio", period="2026-09",
             as_of=date(2026, 10, 1), source_id="em", method=Method.REPORTED)
    assert "汇率" not in MacroSnapshot([f], as_of=date(2026, 10, 25)).for_prompt()


def test_分析时点是今天时取当前有效版本_不用FRED时区里还没到的日期(fake_fred, store):
    # 联网发现：本机日期跑在美国前面时，realtime_end=今天 会被 FRED 拒绝（HTTP 400）
    from datetime import timedelta
    today = date.today()
    fs = fred.fetch_series_asof("DGS10", today, store)
    assert fake_fred[-1]["realtime_end"] == "9999-12-31"
    assert any(f.key == "rate.ust_10y" for f in fs)
    fred.fetch_series_asof("DGS10", today - timedelta(days=30), store)
    assert fake_fred[-1]["realtime_end"] == (today - timedelta(days=30)).isoformat()


def test_历史行情序列不带版本参数_截到as_of(monkeypatch, store):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    seen = []

    class R:
        status_code = 200

        def json(self):
            return {"observations": [{"date": "2026-10-20", "value": "7700.1"},
                                     {"date": "2026-10-21", "value": "."},
                                     {"date": "2026-10-22", "value": "7757.17"}]}
    monkeypatch.setattr(fred.requests, "get", lambda url, params=None, timeout=None: seen.append(params) or R())
    rows = fred.history("SP500", date(2026, 1, 1), date(2026, 10, 22), store)
    assert rows == [(date(2026, 10, 20), D("7700.1")), (date(2026, 10, 22), D("7757.17"))]
    assert "realtime_start" not in seen[0] and seen[0]["observation_end"] == "2026-10-22"
    assert all(KEY not in p.read_text(encoding="utf-8") for p in store.root.iterdir())
