import json
from datetime import date
from pathlib import Path

import pytest

from ir_agent.sources import tenk

FIX = Path(__file__).parent / "fixtures" / "edgar"
AAPL = (FIX / "aapl_10k_excerpt.htm").read_text(encoding="utf-8")
INCORPORATED = (FIX / "tenk_incorporated.htm").read_text(encoding="utf-8")
SUB = json.loads((FIX / "submissions_0000320193.json").read_text(encoding="utf-8"))


def test_html转文本保留段落():
    assert tenk.html_text("<p>A&nbsp;B</p><div>C</div>") == "A B\nC"


def test_html转文本跳过内嵌XBRL头():
    assert "hidden xbrl header" not in tenk.html_text(AAPL)


def test_目录项不算正文():
    ss = tenk.extract_sections(AAPL, "AAPL")
    assert ss.get("risks").text.startswith("The following summarizes factors")
    assert len(ss.get("risks").text) > 2000


def test_四个Item与竞争小节都抽到():
    ss = tenk.extract_sections(AAPL, "AAPL")
    assert {"business", "risks", "outlook", "market_risk", "core_competency"} <= set(ss.sections)
    assert "industry_view" in ss.missing
    assert "10-K 不设此节" in ss.summary() and "银行" not in ss.summary()
    assert ss.get("core_competency").text.startswith("The markets for the Company’s products")
    assert ss.get("outlook").text.startswith("The following discussion")


def test_页脚不进正文():
    ss = tenk.extract_sections(AAPL, "AAPL")
    assert all("Form 10-K |" not in s.text for s in ss.sections.values())


def test_摘录有上限():
    ss = tenk.extract_sections(AAPL, "AAPL", max_chars=1000)
    assert len(ss.get("risks").text) == 1000 and ss.get("risks").truncated


def test_以引用并入的MDA记缺并说明():
    ss = tenk.extract_sections(INCORPORATED, "XYZ")
    assert "outlook" in ss.missing and "以引用方式并入" in ss.summary()
    assert "business" in ss.sections


def test_as_of早于申报日时取上一份10K():
    t = tenk.latest_10k(SUB, "0000320193", date(2025, 10, 30))
    assert t.report_date == date(2024, 9, 28) and t.filed == date(2024, 11, 1)
    assert t.url == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm"


def test_as_of之前没有10K时返回None():
    assert tenk.latest_10k(SUB, "0000320193", date(2024, 1, 1)) is None


def _sub(rows):
    keys = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument")
    return {"filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(keys)}}}


def test_同期有10KA时叙述章节仍取原版10K():
    # 审查 I1：10-K/A 多只补 Part III（高管薪酬、治理）或附件，主文档没有 Item 1/1A/7
    sub = _sub([("10-K", "2025-10-31", "2025-09-27", "a-3", "k25.htm"),
                ("10-K/A", "2025-01-20", "2024-09-28", "a-2", "ka24.htm"),
                ("10-K", "2024-11-01", "2024-09-28", "a-1", "k24.htm")])
    assert tenk.latest_10k(sub, "1", date(2025, 3, 1)).accession == "a-1"
    assert tenk.latest_10k(sub, "1", date(2025, 11, 15)).report_date == date(2025, 9, 27)


def test_同期只有10KA时才用它():
    sub = _sub([("10-K/A", "2025-01-20", "2024-09-28", "a-2", "ka24.htm")])
    assert tenk.latest_10k(sub, "1", date(2025, 3, 1)).accession == "a-2"


def test_美股章节走10K而不是PDF(monkeypatch):
    from types import SimpleNamespace
    from ir_agent import full
    from ir_agent.sources import annual_pdf, edgar
    monkeypatch.setattr(annual_pdf, "_download", lambda *a, **k: (_ for _ in ()).throw(AssertionError("PDF")))
    monkeypatch.setattr(edgar, "get_text", lambda url, params, source, store: (AAPL, "sid"))
    r = SimpleNamespace(code="AAPL", annual_url="https://www.sec.gov/Archives/x.htm", store=None)
    ss, err = full._load_sections(r)
    assert err == "" and "risks" in ss.sections


def test_美股没有10K时如实说明():
    from types import SimpleNamespace
    from ir_agent import full
    ss, err = full._load_sections(SimpleNamespace(code="AAPL", annual_url="", store=None))
    assert ss is None and "10-K" in err
