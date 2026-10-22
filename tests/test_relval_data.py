"""相对估值的取数：同业与本司走同一条算子链，口径只有一份。不联网。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ir_agent import relval_data
from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.operators import dupont, margins
from ir_agent.periods_spot import spot_period
from ir_agent.relval_data import RelvalUnavailable, gather, obs_from_facts

D = Decimal
AS_OF = date(2026, 10, 21)
SPOT = spot_period(AS_OF)


def F(key, period, value, as_of, unit="元"):
    return Fact(key=key, value=D(str(value)), unit=unit, period=period, as_of=as_of,
                source_id="em_x", method=Method.REPORTED, currency="CNY")


def facts_years(years=(2022, 2023, 2024, 2025)):
    """一家公司逐年的年报事实（营收每年 +10%）。"""
    out = {}
    for i, y in enumerate(years):
        p, disc = f"{y}FY", date(y + 1, 4, 25)
        vals = dict(revenue=100 * 1.1 ** i, net_profit_attr_parent=10, net_profit=11,
                    equity_attr_parent=50, total_equity=60, total_assets=120,
                    cash_and_equivalents=5, short_loan=20, operating_profit=15,
                    interest_expense_fe=1, depreciation_fixed_asset=7)
        for k, v in vals.items():
            out[(k, p)] = F(k, p, round(v, 6), disc)
    return out


def _obs(facts, code="002384"):
    return obs_from_facts(code, "某同业", facts, "2025FY", D(1000), AS_OF)


def test_ROE由TTM利润除净资产算出而不读账本():
    """与倍数口径自洽（ROE = PB ÷ PE）。只有年报数据时，它与账本按年报算的 roe 相等。"""
    led = FactLedger()
    for f in facts_years().values():
        led.put(f)
    margins.compute(led, "2025FY", as_of=AS_OF)
    dupont.compute(led, "2025FY", as_of=AS_OF)
    o = _obs(facts_years())
    assert o.roe == o.net_profit / o.equity_parent
    # 账本的 roe 是净利率 × 周转率 × 权益乘数三连乘，末位有舍入差；直接相除更干净
    assert float(o.roe) == pytest.approx(
        float(led.get("roe", "2025FY", as_of=AS_OF).value), rel=1e-20)


def test_三年增速与一年退路():
    o = _obs(facts_years())
    assert o.growth_basis == "3y"
    assert float(o.growth) == pytest.approx(0.1, rel=1e-4)
    assert _obs(facts_years((2024, 2025))).growth_basis == "1y"


def test_TTM取不到用FY并标注():
    o = _obs(facts_years())
    assert o.np_basis == "FY" and o.net_profit == D(10)


def test_EBITDA与有息负债走同一套合计():
    """EBITDA = 营业利润 + 折旧摊销 + 利息费用；折旧摊销与有息负债由分量合计。"""
    o = _obs(facts_years())
    assert o.ebitda == D(15 + 7 + 1)
    assert o.debt == D(20)
    assert o.minority == D(10)


# ── gather：样本、可比子集、行情快照、剔除说明 ──────────────────────

def _quote(mc):
    return {"market_cap": D(mc), "last": D(50), "code": "", "name": "",
            "as_of": datetime(2026, 10, 21, 15, 0)}


def fake_r(peers: dict[str, dict], comps=("002384",)):
    tgt = FactLedger()
    for f in facts_years().values():
        tgt.put(f)
    mc = F("market_cap", SPOT, 5000, AS_OF)
    px = F("last_price", SPOT, 50, AS_OF)
    tgt.put(mc)
    tgt.put(px)
    members = [SimpleNamespace(code=c, name=f"同业{c}") for c in peers]
    return SimpleNamespace(
        period="2025FY", ledger=tgt, quotes=SimpleNamespace(facts=[mc, px]),
        peer_statements={"600183": facts_years(), **peers},
        peer_set=SimpleNamespace(target="600183", target_name="生益科技", members=members,
                                 classification=SimpleNamespace(used_path="电子-元件-印制电路板")),
        comps_codes=list(comps))


def _patch_quotes(monkeypatch, codes):
    def fake(symbols, **kw):
        got = {c: _quote(1000) for c in codes}
        return got, [("URL", "RAW")]
    monkeypatch.setattr(relval_data, "fetch_quotes", fake)


def test_本司与样本口径(monkeypatch, tmp_path):
    _patch_quotes(monkeypatch, ["600183", "002384", "002463"])
    tgt, sample, comps, shares, price, notes = gather(
        fake_r({"002384": facts_years(), "002463": facts_years()}), AS_OF, tmp_path)
    assert tgt.code == "600183" and tgt.market_cap == D(5000)
    assert shares == D(100) and price == D(50)          # 市值 ÷ 现价
    assert {o.code for o in sample} == {"002384", "002463"}
    assert [o.code for o in comps] == ["002384"]        # 可比是样本里的子集，同一份观测


def test_行情原始响应落快照(monkeypatch, tmp_path):
    _patch_quotes(monkeypatch, ["600183", "002384"])
    gather(fake_r({"002384": facts_years()}), AS_OF, tmp_path)
    assert any(p.name.startswith("tencent_batch_") for p in tmp_path.iterdir())


def test_停牌缺行情的同业被剔除并说明(monkeypatch, tmp_path):
    _patch_quotes(monkeypatch, ["600183"])
    _, sample, _, _, _, notes = gather(fake_r({"002384": facts_years()}), AS_OF, tmp_path)
    assert all(o.code != "002384" for o in sample)
    assert any("002384" in n and "行情" in n for n in notes)


def test_未披露本期财报的同业剔除而不用上期凑(monkeypatch, tmp_path):
    _patch_quotes(monkeypatch, ["600183", "002463"])
    _, sample, _, _, _, notes = gather(
        fake_r({"002463": facts_years((2022, 2023, 2024))}), AS_OF, tmp_path)
    assert all(o.code != "002463" for o in sample)
    assert any("002463" in n and "未披露" in n for n in notes)


def test_没有行业样本时明确不可用(tmp_path):
    r = fake_r({})
    del r.peer_statements
    with pytest.raises(RelvalUnavailable):
        gather(r, AS_OF, tmp_path)


def test_行情接口整体失败时明确不可用(monkeypatch, tmp_path):
    def boom(symbols, **kw):
        raise ConnectionError("网络不通")
    monkeypatch.setattr(relval_data, "fetch_quotes", boom)
    with pytest.raises(RelvalUnavailable):
        gather(fake_r({"002384": facts_years()}), AS_OF, tmp_path)


def test_确定性层把同业数据交给相对估值():
    """行业 46 家的财报在 `_load_industry` 里取过一次；不交出来就得重取（东财有频控）。"""
    import inspect

    from ir_agent import full
    ind = inspect.getsource(full._load_industry)
    assert "r.peer_statements = cache" in ind and "r.peer_set = peers" in ind
    assert ind.index("aggregate(peers") < ind.index("r.peer_statements = cache")
    assert "r.comps_codes = " in inspect.getsource(full._load_comps)


def test_剔除说明只上页面不进摘要():
    """说明里有代码与期间（数字），进摘要会被裸数字审计判掉。"""
    from ir_agent.relval import RelvalResult
    assert "sample_notes" in RelvalResult.__dataclass_fields__
    import inspect
    assert "sample_notes" not in inspect.getsource(RelvalResult.summary)


# ── 审查意见 2、3、6：不静默、口径一致 ────────────────────────────

def test_财报未取到的同业也要留下说明(monkeypatch, tmp_path):
    """行业聚合时取数失败的公司不在 cache 里 —— 不能就此从样本里无声消失。"""
    _patch_quotes(monkeypatch, ["600183", "002384"])
    r = fake_r({"002384": facts_years()})
    r.peer_set.members.append(SimpleNamespace(code="002916", name="深南电路"))
    _, _, _, _, _, notes = gather(r, AS_OF, tmp_path)
    assert any("002916" in n and "财报未取到" in n for n in notes)


def test_行情停留在旧日期的同业剔除(monkeypatch, tmp_path):
    """停牌股的行情是旧日期的市值 —— 所有公司必须同一个报价日。"""
    def fake(symbols, **kw):
        return {"002384": {**_quote(1000), "as_of": datetime(2026, 9, 1, 15, 0)}}, [("U", "R")]
    monkeypatch.setattr(relval_data, "fetch_quotes", fake)
    _, sample, _, _, _, notes = gather(fake_r({"002384": facts_years()}), AS_OF, tmp_path)
    assert not sample
    assert any("002384" in n and "停牌" in n for n in notes)


def test_可比名单里没进样本的要说明(monkeypatch, tmp_path):
    _patch_quotes(monkeypatch, ["600183", "002384"])
    _, _, comps, _, _, notes = gather(
        fake_r({"002384": facts_years(), "002463": facts_years()}, comps=("002384", "002463")),
        AS_OF, tmp_path)
    assert [o.code for o in comps] == ["002384"]
    assert any("可比" in n and "002463" in n for n in notes)


def _with_h1(facts):
    """在年报之外加一期 2026 中报（半年流量）。"""
    out = dict(facts)
    for k, v in dict(revenue=70, net_profit_attr_parent=7, net_profit=8,
                     equity_attr_parent=55, total_equity=66, total_assets=130,
                     cash_and_equivalents=6, short_loan=20, operating_profit=9,
                     interest_expense_fe=1, depreciation_fixed_asset=4).items():
        out[(k, "2026H1")] = F(k, "2026H1", v, date(2026, 8, 20))
    return out


def test_中报期的基本面取最近完整财年(monkeypatch, tmp_path):
    """中报的利润、EBITDA 是半年流量，直接算倍数会差一倍；增速也不能锚在未披露的当年 FY。"""
    _patch_quotes(monkeypatch, ["600183", "002384"])
    r = fake_r({"002384": _with_h1(facts_years())})
    r.period = "2026H1"
    tgt, sample, _, _, _, _ = gather(r, AS_OF, tmp_path)
    o = sample[0]
    assert o.net_profit == D(10) and o.ebitda == D(15 + 7 + 1)   # 2025 年报，不是半年数
    assert o.growth_basis == "3y" and float(o.growth) == pytest.approx(0.1, rel=1e-4)
    assert tgt.net_profit == D(10)


# ── PE 分母改用最新 TTM：同业对齐本司截止季度，没出就退上一季，再退年报 ──────

def _with_quarters(facts, upto=("2025Q1", "2025H1", "2025Q1-Q3", "2026Q1", "2026H1")):
    """补归母净利润累计季度数：2025 单季 2/3/2/3（全年 10），2026 单季 4/4。"""
    cum = {"2025Q1": 2, "2025H1": 5, "2025Q1-Q3": 7, "2026Q1": 4, "2026H1": 8}
    disc = {"2025Q1": date(2025, 4, 28), "2025H1": date(2025, 8, 20),
            "2025Q1-Q3": date(2025, 10, 28), "2026Q1": date(2026, 4, 28),
            "2026H1": date(2026, 8, 20)}
    out = dict(facts)
    for per in upto:
        out[("net_profit_attr_parent", per)] = F("net_profit_attr_parent", per, cum[per], disc[per])
    return out


def _target_with_h1(r):
    for f in _with_quarters({}).values():
        r.ledger.put(f)
    r.ledger.put(F("net_profit_ttm", "2026H1", 13, date(2026, 8, 20)))
    return r


def test_本司PE分母取最新TTM(monkeypatch, tmp_path):
    _patch_quotes(monkeypatch, ["600183", "002384"])
    r = _target_with_h1(fake_r({"002384": _with_quarters(facts_years())}))
    tgt, _, _, _, _, _ = gather(r, AS_OF, tmp_path)
    assert tgt.net_profit == D(13) and tgt.np_basis == "TTM 截至 2026H1"


def test_同业对齐本司截止季度(monkeypatch, tmp_path):
    """2025 下半年（2+3）+ 2026 上半年（4+4）= 13。"""
    _patch_quotes(monkeypatch, ["600183", "002384"])
    r = _target_with_h1(fake_r({"002384": _with_quarters(facts_years())}))
    _, sample, _, _, _, _ = gather(r, AS_OF, tmp_path)
    assert sample[0].net_profit == D(13) and sample[0].np_basis == "TTM 截至 2026H1"


def test_同业还没出本季就用上一季TTM(monkeypatch, tmp_path):
    """用户决定：最新一季没出，拿上个季度的 TTM 算。2025Q2–Q4（3+2+3）+ 2026Q1（4）= 12。"""
    _patch_quotes(monkeypatch, ["600183", "002384"])
    peer = _with_quarters(facts_years(), upto=("2025Q1", "2025H1", "2025Q1-Q3", "2026Q1"))
    r = _target_with_h1(fake_r({"002384": peer}))
    _, sample, _, _, _, _ = gather(r, AS_OF, tmp_path)
    assert sample[0].net_profit == D(12) and sample[0].np_basis == "TTM 截至 2026Q1"


def test_季度数据都没有就退回年报(monkeypatch, tmp_path):
    _patch_quotes(monkeypatch, ["600183", "002384"])
    r = _target_with_h1(fake_r({"002384": facts_years()}))
    _, sample, _, _, _, _ = gather(r, AS_OF, tmp_path)
    assert sample[0].net_profit == D(10) and sample[0].np_basis == "FY"


# ── 除 PE 外的输入也取更近的数：存量用最近一期余额，流量用 TTM ─────────────
# A 股折旧摊销只在中报与年报披露（现金流量表补充资料），所以流量 TTM 的截止期
# 只能是 H1 或 FY；半年度 TTM = 本期 H1 + 上年 FY − 上年 H1，恰好只用这两期。

_FLOWS = {
    "revenue":                 {"2022H1": 50, "2022FY": 100, "2023H1": 55, "2023FY": 110,
                                "2024FY": 121, "2025H1": 60, "2025FY": 133.1, "2026H1": 80},
    "operating_profit":        {"2022FY": 10, "2023FY": 11, "2025H1": 7, "2025FY": 15, "2026H1": 9},
    "depreciation_fixed_asset": {"2025H1": 3, "2025FY": 7, "2026H1": 4},
    "interest_expense_fe":     {"2025H1": 0.4, "2025FY": 1, "2026H1": 0.5},
}
_BALANCE = {
    "equity_attr_parent": {"2025FY": 50, "2026H1": 55},
    "total_equity":       {"2025FY": 60, "2026H1": 66},
    "total_assets":       {"2025FY": 120, "2026H1": 130},
    "cash_and_equivalents": {"2025FY": 5, "2026H1": 6},
    "short_loan":         {"2025FY": 20, "2026H1": 22},
}
_DISC = {"2022H1": date(2022, 8, 20), "2022FY": date(2023, 4, 25),
         "2023H1": date(2023, 8, 20), "2023FY": date(2024, 4, 25),
         "2024FY": date(2025, 4, 25), "2025H1": date(2025, 8, 20),
         "2025FY": date(2026, 4, 25), "2026H1": date(2026, 8, 20)}


def facts_full(skip_2026h1=False):
    """年报序列 + 2026 中报；skip_2026h1 模拟同业还没出中报。"""
    out = {}
    for key, by_per in {**_FLOWS, **_BALANCE}.items():
        for per, v in by_per.items():
            if skip_2026h1 and per == "2026H1":
                continue
            out[(key, per)] = F(key, per, v, _DISC[per])
    cum = {"2025H1": 5, "2025FY": 10, "2026H1": 8}
    for per, v in cum.items():
        if skip_2026h1 and per == "2026H1":
            continue
        out[("net_profit_attr_parent", per)] = F("net_profit_attr_parent", per, v, _DISC[per])
    return out


def _obs_full(**kw):
    return obs_from_facts("002384", "某同业", facts_full(**kw), "2025FY", D(1000), AS_OF,
                          bs_period="2026H1", flow_end=(2026, 2),
                          ttm_end=(2026, 2))


def test_存量取最近一期余额():
    o = _obs_full()
    assert o.equity_parent == D(55) and o.total_equity == D(66)     # 2026 中报，不是年报的 50/60
    assert o.cash == D(6) and o.debt == D(22)
    assert o.minority == D(11)
    assert o.bs_basis == "2026H1"


def test_EBITDA取半年度TTM():
    """营业利润 15−7+9=17；折旧 7−3+4=8；利息 1−0.4+0.5=1.1 → 26.1。"""
    o = _obs_full()
    assert o.ebitda == D("26.1") and o.flow_basis == "TTM 截至 2026H1"


def test_ROE用TTM利润除最新净资产():
    """与倍数口径自洽：ROE = PB ÷ PE。"""
    o = _obs_full()
    assert o.net_profit == D(13) and o.roe == D(13) / D(55)
    from ir_agent.relval import multiples
    m = multiples(o)
    # Decimal 连除末位有舍入差，比到 20 位足以证明两者同源
    assert float(m["pb"] / m["pe"]) == pytest.approx(float(o.roe), rel=1e-20)


def test_增速用TTM营收的三年复合():
    """TTM 营收 133.1−60+80=153.1；三年前同期 100−50+55=105。"""
    o = _obs_full()
    assert o.growth_basis == "TTM 3y"
    assert float(o.growth) == pytest.approx((153.1 / 105) ** (1 / 3) - 1, rel=1e-6)


def test_同业没出本期就退上一期并标注():
    o = _obs_full(skip_2026h1=True)
    assert o.equity_parent == D(50) and o.bs_basis == "2025FY"
    assert o.ebitda == D(15 + 7 + 1) and o.flow_basis == "TTM 截至 2025FY"


def test_gather给出本司的存量与流量截止期(monkeypatch, tmp_path):
    _patch_quotes(monkeypatch, ["600183", "002384"])
    r = fake_r({"002384": facts_full()})
    for f in facts_full().values():
        r.ledger.put(f)
    tgt, sample, _, _, _, _ = gather(r, AS_OF, tmp_path)
    assert tgt.bs_basis == "2026H1" and tgt.flow_basis == "TTM 截至 2026H1"
    assert sample[0].bs_basis == "2026H1"
