"""申报文件的完整 XBRL 实例：companyfacts 不含的维度事实（分部收入、按类别的在外股数）。不联网。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

from ir_agent.ledger import FactLedger
from ir_agent.sources import xbrl_instance as X

FILED = date(2025, 10, 31)


def _ctx(cid, start, end, dims=()):
    mem = "".join(f'<xbrldi:explicitMember dimension="{a}">{m}</xbrldi:explicitMember>' for a, m in dims)
    seg = f"<xbrli:segment>{mem}</xbrli:segment>" if dims else ""
    per = (f"<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>" if start
           else f"<xbrli:instant>{end}</xbrli:instant>")
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier>'
            f"{seg}</xbrli:entity><xbrli:period>{per}</xbrli:period></xbrli:context>")


REV = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"


def _doc(facts: list[tuple[str, str, str]], ctxs: list[str]) -> str:
    body = "".join(f'<{t} contextRef="{c}" unitRef="usd" decimals="-6">{v}</{t}>' for t, c, v in facts)
    return ('<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
            'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:us-gaap="http://fasb.org/us-gaap/2025" '
            'xmlns:dei="http://xbrl.sec.gov/dei/2025">' + "".join(ctxs) + body + "</xbrli:xbrl>")


AAPL = _doc(
    [(REV, "fy", "416161000000"), (REV, "iphone", "209586000000"), (REV, "svc", "109158000000"),
     (REV, "iphone_q4", "49025000000"), (REV, "china", "64377000000"),
     ("us-gaap:OperatingIncomeLoss", "china", "26000000000"), (REV, "us", "151790000000"),
     (REV, "two_dims", "1")],
    [_ctx("fy", "2024-09-29", "2025-09-27"),
     _ctx("iphone", "2024-09-29", "2025-09-27", [("srt:ProductOrServiceAxis", "aapl:IPhoneMember")]),
     _ctx("svc", "2024-09-29", "2025-09-27", [("srt:ProductOrServiceAxis", "us-gaap:ServiceMember")]),
     _ctx("iphone_q4", "2025-06-29", "2025-09-27", [("srt:ProductOrServiceAxis", "aapl:IPhoneMember")]),
     _ctx("china", "2024-09-29", "2025-09-27", [("srt:ConsolidationItemsAxis", "us-gaap:OperatingSegmentsMember"),
                                                  ("us-gaap:StatementBusinessSegmentsAxis", "aapl:GreaterChinaSegmentMember")]),
     _ctx("us", "2024-09-29", "2025-09-27", [("srt:StatementGeographicalAxis", "country:US")]),
     _ctx("two_dims", "2024-09-29", "2025-09-27", [("srt:ProductOrServiceAxis", "aapl:IPhoneMember"),
                                                    ("srt:StatementGeographicalAxis", "country:US")])])


def _led(fs):
    led = FactLedger()
    for f in fs:
        led.put(f)
    return led


def test_分部收入按轴与成员入账_单季三个月不入():
    fs = X.parse_instance(AAPL, "0927", "inst_sid", FILED)
    led = _led(fs)
    assert led.get("seg_rev.product.IPhone", "2025FY", as_of=FILED).value == D("209586000000")
    assert led.get("seg_rev.product.Service", "2025FY", as_of=FILED).value == D("109158000000")
    assert led.get("seg_rev.segment.GreaterChinaSegment", "2025FY", as_of=FILED).value == D("64377000000")
    assert led.get("seg_op.segment.GreaterChinaSegment", "2025FY", as_of=FILED).value == D("26000000000")
    assert led.get("seg_rev.geo.US", "2025FY", as_of=FILED).value == D("151790000000")
    f = led.get("seg_rev.product.IPhone", "2025FY", as_of=FILED)
    assert f.unit == "美元" and f.currency == "USD" and f.as_of == FILED and f.source_id == "inst_sid"
    assert {x.period for x in fs} == {"2025FY"}                  # 三个月的 Q4 单季不入账
    assert not any(x.key == "seg_rev.product.IPhone" and x.value == D(1) for x in fs)  # 双维度交叉不收


def test_不带维度的事实不重复入账():
    assert not any(f.key == "revenue" for f in X.parse_instance(AAPL, "0927", "s", FILED))


GOOGL = _doc(
    [("dei:EntityCommonStockSharesOutstanding", "a", "5800000000"),
     ("dei:EntityCommonStockSharesOutstanding", "b", "860000000"),
     ("dei:EntityCommonStockSharesOutstanding", "c", "5400000000")],
    [_ctx(c, None, "2026-07-20", [("us-gaap:StatementClassOfStockAxis", f"us-gaap:{m}")])
     for c, m in (("a", "CommonClassAMember"), ("b", "CommonClassBMember"), ("c", "CapitalClassCMember"))])


def test_多类股按类别加总成在外股数():
    fs = [f for f in X.parse_instance(GOOGL, "1231", "s", date(2026, 7, 24)) if f.key == "shares_outstanding"]
    assert len(fs) == 1 and fs[0].value == D("12060000000") and fs[0].unit == "股"
    assert fs[0].as_of == date(2026, 7, 24)
    assert len(fs[0].derived_from) == 3


def test_有不带维度的封面股数时不另加总():
    doc = _doc([("dei:EntityCommonStockSharesOutstanding", "plain", "100"),
                ("dei:EntityCommonStockSharesOutstanding", "a", "60")],
               [_ctx("plain", None, "2026-07-20"),
                _ctx("a", None, "2026-07-20", [("us-gaap:StatementClassOfStockAxis", "us-gaap:CommonClassAMember")])])
    assert not any(f.key == "shares_outstanding" for f in X.parse_instance(doc, "1231", "s", FILED))


SUB = {"filings": {"recent": {
    "form": ["10-Q", "10-Q", "10-K", "10-Q", "8-K", "10-K"],
    "accessionNumber": ["q3", "q2", "k25", "q1old", "e", "k24"],
    "filingDate": ["2026-08-01", "2026-05-02", "2025-10-31", "2025-08-01", "2025-10-30", "2024-11-01"],
    "reportDate": ["2026-06-27", "2026-03-28", "2025-09-27", "2025-06-28", "", "2024-09-28"],
    "primaryDocument": ["a.htm"] * 6}}}


def test_取最近一份年报及其后的季报_不看时点之后的():
    assert [f.accession for f in X.recent_filings(SUB, date(2026, 10, 26))] == ["k25", "q2", "q3"]
    assert [f.accession for f in X.recent_filings(SUB, date(2026, 6, 1))] == ["k25", "q2"]
    assert [f.accession for f in X.recent_filings(SUB, date(2025, 1, 1))] == ["k24"]


def test_实例文件名_内联XBRL优先():
    items = [{"name": "aapl-20250927.htm"}, {"name": "aapl-20250927_cal.xml"},
             {"name": "aapl-20250927_htm.xml"}, {"name": "FilingSummary.xml"}]
    assert X.instance_name(items) == "aapl-20250927_htm.xml"
    old = [{"name": "abc-20180929.xml"}, {"name": "abc-20180929_lab.xml"}, {"name": "FilingSummary.xml"}]
    assert X.instance_name(old) == "abc-20180929.xml"
    assert X.instance_name([{"name": "x.htm"}]) is None


def test_目录标签给出中文名():
    from ir_agent.analyst import catalog_label
    assert catalog_label("seg_rev.product.IPhone") == "分部营收（产品：IPhone）"
    assert catalog_label("seg_op.segment.GreaterChinaSegment") == "分部营业利润（经营分部：GreaterChinaSegment）"
    assert catalog_label("seg_rev.geo.US") == "分部营收（地区：US）"
    assert catalog_label("revenue") != "revenue"


def test_逐份取实例_比较期重报不记新版本_单份失败只记说明(monkeypatch, tmp_path):
    from ir_agent.sources import edgar
    from ir_agent.sources.snapshot import SnapshotStore
    sub = {"filings": {"recent": {"form": ["10-Q", "10-K"], "accessionNumber": ["0001-26-1", "0001-25-9"],
                                  "filingDate": ["2026-02-01", "2025-10-31"], "reportDate": ["", ""],
                                  "primaryDocument": ["q.htm", "k.htm"]}}}

    def idx(url, source, store, as_of, **kw):
        if "000126" in url:
            raise RuntimeError("boom")
        return {"directory": {"item": [{"name": "k_htm.xml"}]}}, "idx"
    monkeypatch.setattr(edgar, "fetch_json_cached", idx)
    monkeypatch.setattr(edgar, "fetch_text_cached", lambda url, source, store, as_of: (AAPL, "inst"))
    notes = []
    fs = X.load_dimensional("320193", sub, "0927", SnapshotStore(tmp_path), date(2026, 10, 26), notes)
    assert any(f.key == "seg_rev.product.IPhone" for f in fs)
    assert len(notes) == 1 and "0001-26-1" in notes[0] and "RuntimeError" in notes[0]
