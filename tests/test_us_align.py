"""美股同业期间按期末日期对齐（苹果 9 月、戴尔 1 月、Costco 8 月财年）。"""

from datetime import date
from decimal import Decimal as D

from ir_agent.ledger import Fact, Method
from ir_agent.sources.us_peers import PeerBook
from ir_agent.us_align import align_fy, align_quarter, calendarize

AAPL = {"2024FY": date(2024, 9, 28), "2025FY": date(2025, 9, 27), "2026H1": date(2026, 3, 28),
        "2026Q1-Q3": date(2026, 6, 27)}
DELL = {"2024FY": date(2024, 2, 2), "2025FY": date(2025, 1, 31), "2026FY": date(2026, 1, 30),
        "2027Q1": date(2026, 5, 1), "2027H1": date(2026, 7, 31)}


def test_年度取期末最近的财年_不按同名标签():
    assert align_fy(DELL, AAPL["2025FY"], set(DELL)) == "2026FY"       # 4 个月 vs 8 个月
    assert align_fy(DELL, AAPL["2024FY"], set(DELL)) == "2025FY"


def test_年度只在可见的期间里挑_退到半年外就不对齐():
    # 戴尔 FY2026 尚未申报时，FY2025（2025-01-31）距苹果 FY2025 末 239 天 > 半年：不拿它凑
    assert align_fy(DELL, AAPL["2025FY"], set(DELL) - {"2026FY"}) is None


def test_重述后仍保留分析时点可见的那一版():
    book = PeerBook(ticker="DELL", name="Dell", cik="1", fye="0131", ends=DELL, facts=[
        _f("revenue", "2026FY", 95, date(2026, 3, 20)), _f("revenue", "2026FY", 96, date(2027, 3, 20))])
    assert calendarize(book, AAPL, date(2026, 10, 26))[("revenue", "2025FY")].value == D(95)
    assert calendarize(book, AAPL, date(2027, 4, 1))[("revenue", "2025FY")].value == D(96)


def test_年度距离超过半年不对齐():
    assert align_fy({"2024FY": date(2024, 1, 31)}, date(2025, 9, 27), {"2024FY"}) is None


def test_季度取不晚于本司截止季的最新一期():
    assert align_quarter(DELL, AAPL["2026Q1-Q3"], set(DELL)) == ("2027H1", False)   # 7-31 vs 6-27：同一季


def test_同业还没出本季就退一季并标注():
    assert align_quarter(DELL, AAPL["2026Q1-Q3"], set(DELL) - {"2027H1"}) == ("2027Q1", True)


def test_同业出了更新的一季也只取与本司同季的():
    ends = dict(DELL, **{"2027Q1-Q3": date(2026, 10, 30)})
    assert align_quarter(ends, AAPL["2026Q1-Q3"], set(ends)) == ("2027H1", False)


def test_落后超过一季不对齐():
    assert align_quarter({"2026FY": date(2026, 1, 30)}, AAPL["2026Q1-Q3"], {"2026FY"}) is None


def _f(key, period, v, as_of):
    return Fact(key=key, value=D(v), unit="美元", currency="USD", period=period, as_of=as_of,
                source_id="cf_dell", method=Method.REPORTED)


def test_年度数重贴到本司财年标签_季度不搬():
    book = PeerBook(ticker="DELL", name="Dell", cik="1", fye="0131", ends=DELL, facts=[
        _f("revenue", "2026FY", 95, date(2026, 3, 20)), _f("revenue", "2025FY", 88, date(2025, 3, 20)),
        _f("equity_attr_parent", "2026FY", -3, date(2026, 3, 20)),
        _f("revenue", "2027H1", 50, date(2026, 9, 5))])
    cal = calendarize(book, AAPL, date(2026, 10, 26))
    assert cal[("revenue", "2025FY")].value == D(95) and cal[("revenue", "2024FY")].value == D(88)
    assert cal[("equity_attr_parent", "2025FY")].value == D(-3)
    f = cal[("revenue", "2025FY")]
    assert f.as_of == date(2026, 3, 20) and f.source_id == "cf_dell"     # 时点仍是同业自己的申报日
    assert all(p.endswith("FY") for _, p in cal)
