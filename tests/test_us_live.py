"""美股数据源的联网核对（`pytest -m live`）。需要 SEC_USER_AGENT 等环境变量。

阶段 1 的教训：手工固件只能证明代码符合「我对接口的理解」，理解本身对不对
要靠这里（FRED 版本截断、Costco 的 12 周季度、BRK-B 的陈旧股数都是联网才发现的）。
"""
import os
from datetime import date

import pytest

pytestmark = pytest.mark.live

TODAY = date.today()


@pytest.fixture
def store(tmp_path):
    from ir_agent.sources.snapshot import SnapshotStore
    return SnapshotStore(tmp_path)


@pytest.fixture(autouse=True)
def _need_identity():
    if not os.environ.get("SEC_USER_AGENT"):
        pytest.skip("未设置 SEC_USER_AGENT")


def test_苹果最新10K四个Item都抽到(store):
    from ir_agent.sources import edgar, tenk
    sub, _ = edgar.fetch_submissions("0000320193", store)
    t = tenk.latest_10k(sub, "0000320193", TODAY)
    html, _ = edgar.get_text(t.url, {}, "sec_10k", store)
    ss = tenk.extract_sections(html, "AAPL", max_chars=10**7)
    assert {"business", "risks", "outlook", "market_risk"} <= set(ss.sections)
    assert len(ss.get("risks").text) > 5000


def test_苹果近一年业绩8K至少四份(store):
    from ir_agent.sources import edgar
    from ir_agent.sources.edgar_filings import gather_us
    from ir_agent.sources.external import Corpus
    sub, _ = edgar.fetch_submissions("0000320193", store)
    res = gather_us(sub=sub, as_of=TODAY, corpus=Corpus(as_of=TODAY), store=store, company="Apple Inc.")
    earnings = [d for d in res.docs if "业绩发布" in d.title]
    assert len(earnings) >= 4 and all("Apple" in d.text[:2000] for d in earnings)
    assert res.fetch_failed == 0


def test_苹果电话会纪要取到两篇(store):
    from ir_agent.sources.transcripts import fetch_transcripts
    docs, err = fetch_transcripts("AAPL", TODAY, store)
    assert err == "" and len(docs) == 2 and all(len(d.text) > 10000 for d in docs)


def test_苹果一致预期至少两个财年_CAT不可用(store):
    from ir_agent.sources import fmp
    if not os.environ.get("FMP_API_KEY"):
        pytest.skip("未设置 FMP_API_KEY")
    c = fmp.fetch_consensus("AAPL", "0927", TODAY, store)
    assert c is not None and len(c.eps) >= 2 and all(e.n >= 2 for e in c.eps.values())
    assert fmp.fetch_consensus("CAT", "1231", TODAY, store) is None


def test_FOMC最近一份声明(store):
    from ir_agent.sources import fomc
    docs, err = fomc.fetch_statements(TODAY, store, n=1)
    assert err == "" and "Committee" in docs[0].text


def test_FRED企业利润与库存销售比(store):
    from ir_agent.sources import fred
    if not os.environ.get("FRED_API_KEY"):
        pytest.skip("未设置 FRED_API_KEY")
    cp = fred.fetch_series_asof("CP", TODAY, store)
    isr = fred.fetch_series_asof("ISRATIO", TODAY, store)
    assert any(f.key == "macro.corporate_profits_yoy" for f in cp)
    assert all(1 < f.value < 2 for f in isr if f.key == "macro.inventory_sales_ratio")


def test_FRED广义美元指数(store):
    from ir_agent.sources import fred
    if not os.environ.get("FRED_API_KEY"):
        pytest.skip("未设置 FRED_API_KEY")
    fs = fred.fetch_series_asof("TWEXBGSMTH", TODAY, store)
    lvl = max((f for f in fs if f.key == "macro.usd_index"), key=lambda f: f.period)
    assert 80 < lvl.value < 160 and any(f.key == "macro.usd_index_yoy" for f in fs)


def test_英伟达估值供数_拆股还原且两层对账一致(tmp_path):
    import sys
    from ir_agent import dcf_data_us as M, pipeline
    sys.path.insert(0, str(M.US_DCF_DIR))
    import us_calibrate as C
    r = pipeline.run("NVDA", 2025, TODAY, snapshot_dir=tmp_path)
    fye = r.store.load(r.sec_meta["submissions_sid"]).payload["fiscalYearEnd"]
    sid = next(f.source_id for f in r.financials.facts if f.source_id.startswith("sec_companyfacts"))
    mi = M.market_inputs("NVDA", TODAY, r.store, r.ledger)
    assert ("2024-06-10", 10.0) in mi["splits"]
    sh = max((f for (k, _p), fs in r.ledger._facts.items() if k == "shares_outstanding" for f in fs),
             key=lambda f: f.as_of)
    cal = C.calibrate("NVDA", facts=M.pit_us_gaap(r.store.load(sid).payload, TODAY),
                      shares=float(sh.value) / 1e6, splits=mi["splits"], quote=mi["quote"],
                      px=mi["px"], mkt=mi["mkt"])
    dil = cal["stmts"]["shares"]["摊薄股数"].tolist()
    assert max(dil) / min(dil) < 1.2                       # 拆股已还原：五年股数同一量级
    assert M.reconcile(cal["stmts"] | {"ends": cal["periods"]["annual"]}, r.ledger, TODAY, fye) == []
