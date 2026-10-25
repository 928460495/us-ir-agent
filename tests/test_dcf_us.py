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
