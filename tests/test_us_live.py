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
