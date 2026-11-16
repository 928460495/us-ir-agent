"""美股估值同时给出加回 SBC 口径（spec 2026-11-16 C）：主结论 GAAP，加回口径只用于与卖方调整后口径对账。不联网。"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from ir_agent import dcf

AS_OF = date(2026, 11, 15)
FIX = Path(__file__).resolve().parents[1] / "us-dcf" / "tests" / "fixtures" / "AAPL_DCF.xlsx"


def _res(code="AAPL", addback=None):
    return dcf.DcfResult(code=code, workbook="/tmp/x.xlsx", price=D("100"), value_per_share=D("120"),
                         wacc=D("0.09"), terminal_roic=D("0.2"), reinvest_rate=D("0.1"),
                         terminal_share=D("0.6"), verified=True, as_of=AS_OF,
                         value_per_share_sbc_addback=addback)


def test_加回口径独立重算且高于GAAP口径():
    import sys
    sys.path.insert(0, str(FIX.parents[2]))
    import us_verify as V
    gaap = V.recompute(V.read_inputs(str(FIX)))[1]["ps"]
    add = dcf._sbc_addback_ps(str(FIX))
    assert add is not None and float(add) > gaap


def test_摘要写明仅用于对账_并落账本():
    s = _res(addback=D("150")).summary()
    assert "加回股权激励" in s and "仅用于" in s and "[[valuation.value_per_share_sbc_addback@2026-11-15]]" in s
    keys = {f.key for f in dcf.to_facts(_res(addback=D("150")), AS_OF)}
    assert "valuation.value_per_share_sbc_addback" in keys


def test_没有加回口径时不出这一行():
    assert "加回股权激励" not in _res().summary()
    assert "valuation.value_per_share_sbc_addback" not in {f.key for f in dcf.to_facts(_res(), AS_OF)}
    assert "加回股权激励" not in _res(code="600183").summary()


def test_run里接上了且只在美股与验算通过时算():
    src = inspect.getsource(dcf.run)
    assert "_sbc_addback_ps(" in src and "not fails" in src
