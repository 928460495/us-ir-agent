from datetime import date
from decimal import Decimal as D

from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.operators import aggregates, ratios
from ir_agent import relval_data

AS_OF = date(2026, 10, 25)


def ledger(currency, period="2025FY", **vals):
    led = FactLedger()
    unit = "美元" if currency == "USD" else "元"
    for k, v in vals.items():
        led.put(Fact(key=k, value=D(v), unit=unit, currency=currency, period=period,
                     as_of=date(2026, 2, 1), source_id="s", method=Method.REPORTED))
    return led


def test_美股EBIT等于营业利润():
    led = ledger("USD", operating_profit=100, interest_expense_fe=10)
    assert aggregates.ebit(led, "2025FY", AS_OF) == D(100)


def test_A股EBIT仍加回利息():
    led = ledger("CNY", operating_profit=100, interest_expense_fe=10)
    assert aggregates.ebit(led, "2025FY", AS_OF) == D(110)


def test_币种缺失时按A股口径():
    led = ledger(None, operating_profit=100, interest_expense_fe=10)
    assert aggregates.ebit(led, "2025FY", AS_OF) == D(110)


_BS = dict(operating_profit=100, income_tax=20, total_profit=100, total_equity=500,
           equity_attr_parent=500, cash_and_equivalents=100, long_loan=100,
           depreciation_fixed_asset=30, market_cap=1000)


def test_美股ROIC与EV_EBITDA不加利息且不要求利息在场():
    led = ledger("USD", **_BS)                       # 没有 interest_expense_fe（苹果即如此）
    rs = ratios.compute(led, "2025FY", AS_OF)
    # ROIC = 100 × (1 − 0.2) / (500 + 100 − 100)
    assert rs.facts["roic"].value == D(80) / D(500)
    # EV/EBITDA = (1000 + 100 − 100 + 0) / (100 + 30)
    assert rs.facts["ev_ebitda"].value == D(1000) / D(130)


def test_美股有利息时也不加回():
    led = ledger("USD", interest_expense_fe=10, **_BS)
    rs = ratios.compute(led, "2025FY", AS_OF)
    assert rs.facts["roic"].value == D(80) / D(500)
    assert rs.facts["ev_ebitda"].value == D(1000) / D(130)


def test_A股ROIC与EV_EBITDA仍加回利息():
    led = ledger("CNY", interest_expense_fe=10, **_BS)
    rs = ratios.compute(led, "2025FY", AS_OF)
    assert rs.facts["roic"].value == D(110) * D("0.8") / D(500)
    assert rs.facts["ev_ebitda"].value == D(1000) / D(140)


def test_美股相对估值EBITDA不含利息且不要求利息在场():
    led = ledger("USD", **_BS)
    obs = relval_data.obs_from_ledger("AAPL", "Apple", led, "2025FY", AS_OF, D(1000))
    assert obs.ebitda == D(130)


def test_A股相对估值EBITDA仍加回利息():
    led = ledger("CNY", interest_expense_fe=10, **_BS)
    obs = relval_data.obs_from_ledger("600183", "生益科技", led, "2025FY", AS_OF, D(1000))
    assert obs.ebitda == D(140)


def test_美股DOL与DFL用营业利润作EBIT():
    from ir_agent import industry_profile as ip
    led = ledger("USD", operating_profit=100, interest_expense_fe=10, admin_expense=50)
    assert ip.dol(led, "2025FY", AS_OF) == D(1) + D(50) / D(100)
    assert ip.dfl(led, "2025FY", AS_OF) == D(100) / D(90)


def test_美股AltmanZ的C项不加利息():
    from ir_agent.operators import quality
    vals = dict(current_assets=400, current_liabilities=200, total_assets=1000,
                retained_earnings=300, operating_profit=100, total_liabilities=500,
                revenue_total=800, market_cap=1500, interest_expense_fe=10)
    z_us, _ = quality.altman_z(ledger("USD", **vals), "2025FY", AS_OF)
    z_cn, _ = quality.altman_z(ledger("CNY", **vals), "2025FY", AS_OF)
    assert z_cn.value - z_us.value == D("3.3") * D(10) / D(1000)
