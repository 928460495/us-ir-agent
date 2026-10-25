from datetime import date
from decimal import Decimal as D

import pandas as pd
import pytest

from ir_agent import dcf_data_us as M
from ir_agent.ledger import Fact, FactLedger, Method


def _e(end, val, filed, start=None):
    x = {"end": end, "val": val, "filed": filed, "form": "10-K", "fy": 2025, "fp": "FY"}
    if start:
        x["start"] = start
    return x


DOC = {"facts": {"us-gaap": {
    "Revenues": {"units": {"USD": [_e("2024-12-31", 100, "2025-02-01", "2024-01-01"),
                                   _e("2024-12-31", 90, "2025-06-01", "2024-01-01"),      # 修订，晚于 as_of
                                   _e("2025-12-31", 120, "2026-02-01", "2025-01-01")]}}},
    "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [_e("2025-01-20", 5, "2025-02-01")]}}}}}


def test_按时点过滤companyfacts():
    g = M.pit_us_gaap(DOC, date(2025, 3, 1))
    vals = [x["val"] for x in g["Revenues"]["units"]["USD"]]
    assert vals == [100]                                    # 修订与之后的年报都不可见


def test_拆股记录从Tiingo日线提取():
    rows = [{"date": "2024-06-07T00:00:00+00:00", "splitFactor": 1.0},
            {"date": "2024-06-10T00:00:00+00:00", "splitFactor": 10.0},
            {"date": "2024-06-11T00:00:00+00:00", "splitFactor": 1.0}]
    assert M.splits_from_tiingo(rows) == [("2024-06-10", 10.0)]


def test_日线转成收盘价表():
    rows = [{"date": "2026-10-22T00:00:00+00:00", "adjClose": 100.0, "close": 100.0},
            {"date": "2026-10-23T00:00:00+00:00", "adjClose": 101.0, "close": 101.0}]
    df = M.close_frame(rows, "adjClose")
    assert list(df.columns) == ["Close"] and df["Close"].iloc[-1] == 101.0
    assert df.index[0] == pd.Timestamp("2026-10-22")


def _ledger(fy: dict):
    led = FactLedger()
    for (key, period), v in fy.items():
        led.put(Fact(key=key, value=D(v), unit="美元", currency="USD", period=period,
                     as_of=date(2026, 2, 1), source_id="s", method=Method.REPORTED))
    return led


def test_报表与账本对账_一致时无告警():
    frames = {"income": pd.DataFrame({"营业收入": [120_000.0]}, index=["2025-12-31"]),
              "balance": pd.DataFrame({"资产总计": [500_000.0]}, index=["2025-12-31"]),
              "ends": ["2025-12-31"]}
    led = _ledger({("revenue", "2025FY"): "120000000000", ("total_assets", "2025FY"): "500000000000"})
    assert M.reconcile(frames, led, date(2026, 6, 1), "1231") == []


def test_报表与账本对账_差异写进告警():
    frames = {"income": pd.DataFrame({"营业收入": [125_000.0]}, index=["2025-12-31"]),
              "balance": pd.DataFrame({"资产总计": [500_000.0]}, index=["2025-12-31"]),
              "ends": ["2025-12-31"]}
    led = _ledger({("revenue", "2025FY"): "120000000000", ("total_assets", "2025FY"): "500000000000"})
    w = M.reconcile(frames, led, date(2026, 6, 1), "1231")
    assert len(w) == 1 and "营业收入" in w[0] and "2025FY" in w[0] and "4.17%" in w[0]


def test_calibrate注入报表事实时不自行取companyfacts(monkeypatch):
    import sys
    sys.path.insert(0, str(M.US_DCF_DIR))
    import us_calibrate as C
    import us_data as UD
    g = {"Revenues": {"units": {"USD": []}}}
    seen = {}
    monkeypatch.setattr(UD, "resolve", lambda s: dict(cik="0000320193", ticker="AAPL", sic="3571",
                                                     sic_desc="", name="Apple", fye="0927", exchange="Nasdaq"))
    monkeypatch.setattr(UD, "facts", lambda cik: (_ for _ in ()).throw(AssertionError("不该自行取 companyfacts")))

    def statements(gg, ticker, nyears, splits=None):
        seen.update(g=gg, splits=splits)
        raise RuntimeError("到此为止")
    monkeypatch.setattr(UD, "statements", statements)
    with pytest.raises(RuntimeError, match="到此为止"):
        C.calibrate("AAPL", facts=g, shares=15000.0, splits=[("2024-06-10", 10.0)])
    assert seen == {"g": g, "splits": [("2024-06-10", 10.0)]}
