"""ir_agent.dcf 用同一流程驱动 us-dcf（离线：校准打桩为 us-dcf 的 AAPL 固件）。"""
from __future__ import annotations

import importlib.util
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from ir_agent import assume, dcf
from ir_agent.ledger import FactLedger
from ir_agent.methodology import parse as parse_meth

US_DCF = Path(__file__).resolve().parent.parent / "us-dcf"
_spec = importlib.util.spec_from_file_location("us_dcf_conftest", US_DCF / "tests" / "conftest.py")
_cf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cf)
AS_OF = date(2026, 10, 26)
Q = "「依据原文」"


@pytest.fixture
def stub_cal(monkeypatch):
    monkeypatch.setattr(dcf, "_calibrate_us",
                        lambda *a, **k: (_cf.load_cal(), ["报表由同一份 companyfacts 供数"], None))


def _assumptions(cal):
    model = parse_meth("""模式: 变化
因子: 量增 | 出货量同比变化率 | 变化率 | 10-K | 出货量
因子: 价增 | 平均售价同比变化率 | 变化率 | 电话会 | 平均售价
因子: 结构 | 服务占比提升对毛利率的贡献 | pp | 10-K | 服务收入
公式: 营收 = 上期(营收) × (1 + 量增) × (1 + 价增)
公式: 毛利率 = 上期(毛利率) + 结构 / 100
""").model
    rows = {"量增": ("-3%", "2%", "5%"), "价增": ("-2%", "1%", "3%"), "结构": ("0", "0.4", "0.8")}
    years = len(cal["periods"]["proj"])
    text = "\n".join(f"假设: {k} | 第{y}年 | {lo} | {mid} | {hi} | 理由 {Q}"
                     for y in range(1, years + 1) for k, (lo, mid, hi) in rows.items())
    a = assume.parse(text, years, decomps=model)
    a.labels = list(cal["periods"]["proj"])
    return a


def test_美股走us_dcf建表并通过验算(stub_cal, tmp_path):
    res = dcf.run("AAPL", tmp_path, as_of=AS_OF, ledger=FactLedger(), us_ctx={})
    assert res.verified and Path(res.workbook).exists() and "AAPL" in Path(res.workbook).name
    assert set(res.scenario_ps) == {"bear", "base", "bull"}
    cal = _cf.load_cal()
    assert res.value_per_share > 0 and res.price == D(str(cal["meta"]["price"]))
    assert res.beta == D(str(cal["dcf"]["beta"]))
    assert {f.unit for f in dcf.to_facts(res, AS_OF) if f.key == "valuation.value_per_share"} == {"美元"}
    assert res.mc and res.mc["p5"] < res.mc["p50"] < res.mc["p95"]


def test_判断项写入后读回一致_三档分化(stub_cal, tmp_path):
    cal = _cf.load_cal()
    res = dcf.run("AAPL", tmp_path, as_of=AS_OF, ledger=FactLedger(), us_ctx={},
                  assumptions=_assumptions(cal), approved_by="tester")
    assert res.verified
    assert not any("没进工作簿" in w or "不一致" in w for w in res.warnings), res.warnings
    assert res.scenario_ps["bull"] > res.scenario_ps["base"] > res.scenario_ps["bear"]
    assert "首年" in res.anchor_note                         # AAPL 锚定前三季，首年判断项被覆盖


def test_三档有一档验算失败时不交出情景价值(stub_cal, tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(US_DCF))
    import us_verify as V
    real = V.verify_all_scenarios

    def one_bad(path, quiet=False, values=None):
        return real(path, quiet=quiet, values=values) + [("Bear", "终值", "B60", 1.0, 2.0)]
    monkeypatch.setattr(V, "verify_all_scenarios", one_bad)
    res = dcf.run("AAPL", tmp_path, as_of=AS_OF, ledger=FactLedger(), us_ctx={})
    assert res.scenario_ps == {}


def test_A股代码仍走ashare_dcf():
    _, C_, _, _ = dcf._load_for("600183")
    assert C_.__name__ == "ashare_calibrate"
    _, C2, _, V2 = dcf._load_for("AAPL")
    assert C2.__name__ == "us_calibrate" and V2.__name__ == "us_verify"


# ── 审查发现（阶段 3 最终审查）────────────────────────────────────

def _ctx(tmp_path, ledger=None):
    from datetime import datetime
    from ir_agent.ledger import Fact, Method
    from ir_agent.sources.snapshot import SnapshotStore
    st = SnapshotStore(tmp_path / "snap")
    cf = st.save(source="sec_companyfacts", payload={"facts": {"us-gaap": {}}}, url="u", fetched_at=datetime.now())
    sub = st.save(source="sec_submissions", payload={"cik": "320193", "name": "Apple Inc.", "sic": "3571",
                                                     "sicDescription": "Electronic Computers", "fiscalYearEnd": "0927",
                                                     "tickers": ["AAPL"], "exchanges": ["Nasdaq"]},
                  url="u", fetched_at=datetime.now())
    led = ledger or FactLedger()
    if ledger is None:
        led.put(Fact(key="shares_outstanding", value=D("14776353000"), unit="股", period="2025FY",
                     as_of=date(2025, 10, 31), source_id="s", method=Method.REPORTED))
    return led, {"store": st, "companyfacts_sid": cf, "submissions_sid": sub, "fye": "0927"}


def _engine_mods():
    return dcf._load_for("AAPL")


def test_行情取不到时转成DcfUnavailable_不让第一阶段崩(tmp_path, monkeypatch):
    from ir_agent import dcf_data_us as M
    from ir_agent.sources.us_quotes import QuoteUnavailable
    monkeypatch.setattr(M, "market_inputs", lambda *a, **k: (_ for _ in ()).throw(QuoteUnavailable("Tiingo 日线 HTTP 401")))
    led, ctx = _ctx(tmp_path)
    D_, C_, _, _ = _engine_mods()
    with pytest.raises(dcf.DcfUnavailable, match="Tiingo"):
        dcf._calibrate_us(D_, C_, "AAPL", AS_OF, led, 5, ctx)


def test_上下文缺快照时同样转成DcfUnavailable(tmp_path):
    led, _ = _ctx(tmp_path)
    D_, C_, _, _ = _engine_mods()
    with pytest.raises(dcf.DcfUnavailable):
        dcf._calibrate_us(D_, C_, "AAPL", AS_OF, led, 5, {})


def _stub_engine(monkeypatch, diag):
    from ir_agent import dcf_data_us as M
    import us_calibrate as UC
    import us_data as UD
    seen = {}
    monkeypatch.setattr(M, "market_inputs", lambda *a, **k: dict(quote={"source": "t", "price": 1.0}, px=None, mkt=None, splits=[]))
    monkeypatch.setattr(UD, "resolve", lambda s: (_ for _ in ()).throw(AssertionError("直连 SEC：应注入 meta")))

    def calib(code, years, **kw):
        seen.update(kw)
        cal = _cf.load_cal()
        cal["meta"]["diag"] = diag
        return cal
    monkeypatch.setattr(UC, "calibrate", calib)
    return seen


def test_元数据由submissions快照注入_不直连SEC(tmp_path, monkeypatch):
    seen = _stub_engine(monkeypatch, {"缺失": [], "拆股": []})
    led, ctx = _ctx(tmp_path)
    D_, C_, _, _ = _engine_mods()
    dcf._calibrate_us(D_, C_, "AAPL", AS_OF, led, 5, ctx)
    assert seen["meta"]["cik"] == "0000320193" and seen["meta"]["fye"] == "0927" and seen["meta"]["sic"] == "3571"
    assert seen["shares"] == pytest.approx(14776.353)


def test_账本没有在外股数时不去直连SEC补(tmp_path, monkeypatch):
    _stub_engine(monkeypatch, {})
    led, ctx = _ctx(tmp_path, ledger=FactLedger())
    D_, C_, _, _ = _engine_mods()
    with pytest.raises(dcf.DcfUnavailable, match="在外股数"):
        dcf._calibrate_us(D_, C_, "AAPL", AS_OF, led, 5, ctx)


def test_引擎换源与缺失诊断进notes_有换源时不再说同源(tmp_path, monkeypatch):
    _stub_engine(monkeypatch, {"换源": ["营业成本 取自 FMP（XBRL 不单列）"], "缺失": ["夹层权益"], "拆股": []})
    led, ctx = _ctx(tmp_path)
    D_, C_, _, _ = _engine_mods()
    _, notes, _ = dcf._calibrate_us(D_, C_, "AAPL", AS_OF, led, 5, ctx)
    text = "\n".join(notes)
    assert "营业成本 取自 FMP" in text and "夹层权益" in text
    assert "与分析层同源" not in text and "部分科目不同源" in text


def test_美股估值摘要不写上证综指与ashare_dcf():
    res = dcf.DcfResult(code="AAPL", workbook="/tmp/AAPL_DCF.xlsx", price=D("100"), value_per_share=D("120"),
                        wacc=D("0.09"), terminal_roic=D("0.2"), reinvest_rate=D("0.1"),
                        terminal_share=D("0.6"), verified=True, as_of=AS_OF)
    s = res.summary()
    assert "上证" not in s and "ashare" not in s and "标普 500" in s and "us-dcf" in s
    cn = dcf.DcfResult(code="600183", workbook="/tmp/x.xlsx", price=D("10"), value_per_share=D("12"),
                       wacc=D("0.09"), terminal_roic=D("0.2"), reinvest_rate=D("0.1"),
                       terminal_share=D("0.6"), verified=True, as_of=AS_OF).summary()
    assert "上证综指" in cn


def test_美股量级告警写美元():
    w = dcf._scale_warnings(D("1000"), D("10"), money="美元")
    assert w and "1,000.00 美元与现价 10.00 美元" in w[0] and "摊薄股数（百万股）" in w[0]


def test_美股基期营收按百万美元折算_与工作簿同口径():
    from ir_agent.assume import _base_revenue_scale
    from ir_agent.ledger import Fact, Method
    us = Fact(key="revenue", value=D("416161000000"), unit="美元", currency="USD", period="2025FY",
              as_of=date(2025, 10, 31), source_id="s", method=Method.REPORTED)
    cn = Fact(key="revenue", value=D("20000000000"), unit="元", currency="CNY", period="2025FY",
              as_of=date(2026, 3, 1), source_id="s", method=Method.REPORTED)
    assert us.value / _base_revenue_scale(us) == D("416161")
    assert cn.value / _base_revenue_scale(cn) == D("200")


def test_指数名标普500不算裸数字():
    from ir_agent.citation import audit_bare_numbers
    assert audit_bare_numbers("β 对标普 500 回归，对照 S&P 500 指数") == []
    assert audit_bare_numbers("营收 500 亿") != []              # 白名单只放过指数名
