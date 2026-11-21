"""相对估值通用化（spec 2026-11-21-relval-fundamentals）：驱动表、基本面观测、按业务与基本面挑可比。不联网。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ir_agent import relval
from ir_agent.relval import DRIVERS, PeerObs
from ir_agent.relval_data import obs_from_facts
from tests.test_relval_data import AS_OF, F, facts_years

D = Decimal


# ── Task 1：驱动表与基本面观测 ─────────────────────────────────

def test_驱动表覆盖全部五个倍数():
    assert set(DRIVERS) == {"pe", "pe_ntm", "pb", "ev_ebitda", "ev_sales"}
    assert DRIVERS["pe"] == ("growth", "roe3y")
    assert DRIVERS["pb"] == ("roe3y", "growth")
    assert DRIVERS["ev_ebitda"] == ("ebitda_margin", "growth")
    assert DRIVERS["ev_sales"] == ("ebitda_margin", "growth")


def _with_roe(np_by_year: dict[int, float], eq=50):
    out = facts_years()
    for y, npv in np_by_year.items():
        p = f"{y}FY"
        out[("net_profit_attr_parent", p)] = F("net_profit_attr_parent", p, npv, date(y + 1, 4, 25))
        out[("equity_attr_parent", p)] = F("equity_attr_parent", p, eq, date(y + 1, 4, 25))
    return out


def test_三年平均ROE取近三个年报():
    o = obs_from_facts("002384", "某同业", _with_roe({2023: 5, 2024: 10, 2025: 15}),
                       "2025FY", D(1000), AS_OF)
    assert o.roe3y == pytest.approx(D("0.2"))          # (0.1 + 0.2 + 0.3) / 3
    assert o.roe3y_basis == "3y"


def test_三年平均ROE不足三年按实有年数并标注():
    facts = {k: v for k, v in _with_roe({2024: 10, 2025: 20}).items()
             if k[1] in ("2024FY", "2025FY")}
    o = obs_from_facts("002384", "某同业", facts, "2025FY", D(1000), AS_OF)
    assert o.roe3y == pytest.approx(D("0.3"))          # (0.2 + 0.4) / 2
    assert o.roe3y_basis == "2y"


def test_三年平均ROE跳过净资产不为正的年份():
    facts = _with_roe({2023: 5, 2024: 10, 2025: 15})
    facts[("equity_attr_parent", "2023FY")] = F("equity_attr_parent", "2023FY", -1, date(2024, 4, 25))
    o = obs_from_facts("002384", "某同业", facts, "2025FY", D(1000), AS_OF)
    assert o.roe3y == pytest.approx(D("0.25"))
    assert o.roe3y_basis == "2y"


def test_EBITDA利润率等于EBITDA除营收且可为负():
    o = obs_from_facts("002384", "某同业", facts_years(), "2025FY", D(1000), AS_OF)
    assert o.ebitda_margin == o.ebitda / o.revenue
    facts = facts_years()
    facts[("operating_profit", "2025FY")] = F("operating_profit", "2025FY", -40, date(2026, 4, 25))
    o2 = obs_from_facts("002384", "某同业", facts, "2025FY", D(1000), AS_OF)
    assert o2.ebitda_margin is not None and o2.ebitda_margin < 0


def test_A股观测带营收():
    o = obs_from_facts("002384", "某同业", facts_years(), "2025FY", D(1000), AS_OF)
    assert o.revenue is not None and o.revenue > 0
