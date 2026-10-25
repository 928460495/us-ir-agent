import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from ir_agent.ledger import FactLedger, Method
from ir_agent.sources.edgar_facts import parse_companyfacts

FIX = Path(__file__).parent / "fixtures" / "edgar"
AAPL = json.loads((FIX / "companyfacts_0000320193.json").read_text(encoding="utf-8"))


def _x(start, end, val, filed, form="10-K", fy=2025, fp="FY"):
    e = {"end": end, "val": val, "accn": f"acc-{filed}", "fy": fy, "fp": fp, "form": form, "filed": filed}
    if start:
        e["start"] = start
    return e


def doc(gaap: dict, dei: dict | None = None) -> dict:
    """{标签: [条目]} → companyfacts 结构。"""
    def wrap(tags, unit_of):
        return {t: {"units": {unit_of(t): arr}} for t, arr in tags.items()}
    out = {"us-gaap": wrap(gaap, lambda t: "shares" if "Shares" in t else "USD")}
    if dei:
        out["dei"] = wrap(dei, lambda t: "shares")
    return {"cik": 1, "facts": out}


def get(fs, key, period, as_of=date(2099, 1, 1)):
    led = FactLedger()
    for f in fs:
        led.put(f)
    return led.get(key, period, as_of=as_of)


def test_比较期按起止日期标注而非申报财年():
    fs = parse_companyfacts(AAPL, "0927", "sid")
    periods = {f.period for f in fs if f.key == "revenue"}
    assert {"2024FY", "2025FY", "2025Q1", "2025H1"} == periods     # 单季 Q2 三个月不入账


def test_比较期在后一年申报里是新版本():
    fs = parse_companyfacts(AAPL, "0927", "sid")
    vs = sorted(f.as_of for f in fs if f.key == "revenue" and f.period == "2024FY")
    assert vs == [date(2024, 11, 1), date(2025, 10, 31)]


def test_披露日取filed():
    f = get(parse_companyfacts(AAPL, "0927", "sid"), "revenue", "2025FY")
    assert f.as_of == date(2025, 10, 31) and f.value == D("416161000000")


def test_只收定期报告():
    fs = parse_companyfacts(AAPL, "0927", "sid")
    assert date(2025, 10, 30) not in {f.as_of for f in fs}       # 8-K 那条不入账


def test_金额单位为美元():
    fs = parse_companyfacts(AAPL, "0927", "sid")
    assert all(f.unit == "美元" and f.currency == "USD" for f in fs if f.key == "revenue")
    assert all(f.method is Method.REPORTED and f.source_id == "sid" for f in fs)


def test_时点数按截止日标期():
    fs = parse_companyfacts(AAPL, "0927", "sid")
    assert get(fs, "total_assets", "2025Q1").value == D("344085000000")
    assert get(fs, "total_assets", "2025FY").value == D("359241000000")


def test_在外股数用封面期间且单位为股():
    fs = parse_companyfacts(AAPL, "0927", "sid")
    f = get(fs, "shares_outstanding", "2025FY")
    assert f.value == D("14776353000") and f.unit == "股" and f.currency is None
    assert get(fs, "shares_outstanding", "2025Q1-Q3").as_of == date(2025, 8, 1)


def test_修订申报是新版本且时点守卫生效():
    d = doc({"Revenues": [
        _x("2023-10-01", "2024-09-28", 100, "2024-11-01", fy=2024),
        _x("2023-10-01", "2024-09-28", 90, "2025-03-01", form="10-K/A", fy=2024)]})
    fs = parse_companyfacts(d, "0927", "sid")
    assert get(fs, "revenue", "2024FY", as_of=date(2025, 1, 1)).value == D(100)
    assert get(fs, "revenue", "2024FY", as_of=date(2025, 12, 1)).value == D(90)


def test_折旧摊销有合计时只记合计():
    d = doc({"DepreciationDepletionAndAmortization": [_x("2024-09-29", "2025-09-27", 50, "2025-10-31")],
             "Depreciation": [_x("2024-09-29", "2025-09-27", 30, "2025-10-31")],
             "AmortizationOfIntangibleAssets": [_x("2024-09-29", "2025-09-27", 20, "2025-10-31")]})
    fs = parse_companyfacts(d, "0927", "sid")
    da = [f for f in fs if f.period == "2025FY" and f.key in
          ("depreciation_fixed_asset", "amortization_intangible", "amortization_useright")]
    assert [(f.key, f.value) for f in da] == [("depreciation_fixed_asset", D(50))]


def test_折旧摊销无合计时记三个分项():
    d = doc({"Depreciation": [_x("2024-07-01", "2025-06-30", 30, "2025-07-30")],
             "AmortizationOfIntangibleAssets": [_x("2024-07-01", "2025-06-30", 20, "2025-07-30")],
             "FinanceLeaseRightOfUseAssetAmortization": [_x("2024-07-01", "2025-06-30", 5, "2025-07-30")]})
    fs = parse_companyfacts(d, "0630", "sid")
    got = {f.key: f.value for f in fs if f.period == "2025FY"}
    assert got == {"depreciation_fixed_asset": D(30), "amortization_intangible": D(20),
                   "amortization_useright": D(5)}


def test_归母与全口径净利润标签不混():
    d = doc({"ProfitLoss": [_x("2025-01-01", "2025-12-31", 110, "2026-02-14")],
             "NetIncomeLoss": [_x("2025-01-01", "2025-12-31", 100, "2026-02-14")]})
    fs = parse_companyfacts(d, "1231", "sid")
    assert get(fs, "net_profit", "2025FY").value == D(110)
    assert get(fs, "net_profit_attr_parent", "2025FY").value == D(100)


def test_无全口径标签时净利润退回归母():
    fs = parse_companyfacts(AAPL, "0927", "sid")
    assert get(fs, "net_profit", "2025FY").value == D("112010000000")


def test_按覆盖度选主标签_缺期才补并留说明():
    # 主标签 Revenues 覆盖三期；次优标签在 2023FY 与主标签都有，只在 2022FY 补缺
    d = doc({"RevenueFromContractWithCustomerExcludingAssessedTax": [
                 _x("2022-01-01", "2022-12-31", 7, "2023-02-01", fy=2022),
                 _x("2023-01-01", "2023-12-31", 8, "2024-02-01", fy=2023)],
             "Revenues": [
                 _x("2023-01-01", "2023-12-31", 80, "2024-02-01", fy=2023),
                 _x("2024-01-01", "2024-12-31", 90, "2025-02-01", fy=2024),
                 _x("2025-01-01", "2025-12-31", 99, "2026-02-01", fy=2025)]})
    notes = []
    fs = parse_companyfacts(d, "1231", "sid", notes=notes)
    assert get(fs, "revenue", "2023FY").value == D(80)          # 不被次优标签覆盖
    assert get(fs, "revenue", "2022FY").value == D(7)           # 主标签缺期才补
    assert any("revenue" in n and "2022FY" in n and "RevenueFromContract" in n for n in notes)


def test_利息支出不取净额标签():
    d = doc({"InterestIncomeExpenseNet": [_x("2025-01-01", "2025-12-31", 40, "2026-02-14")]})
    assert not [f for f in parse_companyfacts(d, "1231", "sid") if f.key == "interest_expense_fe"]


def test_fetch_statements_us串起代码_财年末_事实(monkeypatch, tmp_path):
    from ir_agent.sources import edgar
    from ir_agent.sources.edgar_facts import fetch_statements_us
    from ir_agent.sources.snapshot import SnapshotStore

    monkeypatch.setenv("SEC_USER_AGENT", "Tester tester@example.org")

    class R:
        def __init__(self, url):
            name = url.rsplit("/", 1)[-1]
            if name == "company_tickers_exchange.json":
                self._b = (FIX / name).read_text(encoding="utf-8")
            else:
                kind = "submissions" if "/submissions/" in url else "companyfacts"
                self._b = (FIX / f"{kind}_{name[3:13]}.json").read_text(encoding="utf-8")

        def raise_for_status(self):
            pass

        def json(self):
            return json.loads(self._b)

    monkeypatch.setattr(edgar.requests, "get", lambda url, headers=None, timeout=None: R(url))
    facts, sids = fetch_statements_us("AAPL", SnapshotStore(tmp_path))
    assert set(sids) == {"submissions", "companyfacts"}
    rev = get(facts, "revenue", "2025FY")
    assert rev.value == D("416161000000") and rev.source_id == sids["companyfacts"]
