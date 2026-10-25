import json
from pathlib import Path

import pytest

from ir_agent.sources import edgar, us_industry
from ir_agent.sources.industry import Peers
from ir_agent.sources.snapshot import SnapshotStore
from ir_agent.sources.us_industry import ff49_of

FIX = Path(__file__).parent / "fixtures" / "edgar"


@pytest.mark.parametrize("sic,ff49", [(3571, 35), (3674, 37), (5331, 43), (3531, 21),
                                      (7372, 36), (6022, 45)])
def test_SIC映射到FF49(sic, ff49):
    assert ff49_of(sic)[0] == ff49


def test_映射表之外的SIC返回None():
    assert ff49_of(9999) is None


def test_定义文件里的区间互不重叠():
    seen = {}
    for lo, hi, num, *_ in us_industry.ff49_table():
        for s in range(lo, hi + 1):
            assert seen.setdefault(s, num) == num, f"SIC {s} 同时属于 {seen[s]} 与 {num}"


def test_行业的SIC清单():
    sics = us_industry.sic_codes_of(35)
    assert 3571 in sics and 3577 in sics and 3674 not in sics


class R:
    status_code = 200

    def __init__(self, text=None, js=None):
        self.text, self._js = text, js
        self.content = (text or json.dumps(js)).encode()

    def raise_for_status(self):
        pass

    def json(self):
        return self._js


SIC_LIST_BROKEN = {"on": False}


@pytest.fixture
def fake_sec(monkeypatch):
    SIC_LIST_BROKEN["on"] = False
    monkeypatch.setenv("SEC_USER_AGENT", "Tester tester@example.org")
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append((url, dict(params or {})))
        if url.endswith("company_tickers_exchange.json"):
            return R(js=json.loads((FIX / "company_tickers_exchange.json").read_text()))
        if "/submissions/" in url:
            return R(js=json.loads((FIX / "submissions_0000320193.json").read_text()))
        if "standard-industrial-classification" in url:
            if SIC_LIST_BROKEN["on"]:
                return R(text="<html>maintenance</html>")
            return R(text=(FIX / "sic_code_list.html").read_text())
        if "browse-edgar" in url:
            p = FIX / f"browse_sic_{params['SIC']}.html"
            if p.exists() and int(params.get("start", 0)) == 0:
                return R(text=p.read_text())
            return R(text="<html><body>No matching companies.</body></html>")
        raise AssertionError(url)
    monkeypatch.setattr(edgar.requests, "get", get)
    return calls


def test_同业名单形状与A股一致(fake_sec, tmp_path):
    ps = us_industry.peer_set_us("AAPL", SnapshotStore(tmp_path))
    assert isinstance(ps, Peers)
    assert ps.target == "AAPL" and ps.target_name == "Apple Inc."
    assert ps.classification.used_path.startswith("FF49-35")
    assert all(m.code != "AAPL" for m in ps.members)
    assert {m.code for m in ps.members} == {"DELL", "HPQ", "SMCI", "FHWC"}


def test_非主板交易所与无代码公司剔除并留原因(fake_sec, tmp_path):
    ps = us_industry.peer_set_us("AAPL", SnapshotStore(tmp_path))
    why = {code: reason for code, _name, reason in ps.excluded}
    assert "OHWC" in why and "OTC" in why["OHWC"]
    # 无交易代码的只给计数，不逐家列（退市空壳可能上千家，会刷屏）—— 审查 I4
    assert not any(code == "0009999999" for code, _n, _r in ps.excluded)
    assert any("无交易代码" in r and "1 家" in r for _c, _n, r in ps.excluded)


def test_描述不写申万(fake_sec, tmp_path):
    ps = us_industry.peer_set_us("AAPL", SnapshotStore(tmp_path))
    s = ps.summary()
    assert "申万" not in s and "Fama-French" in s
    assert ps.thin is True                                    # 四家 < 五家下限


def test_按SIC翻页直到没有新公司(fake_sec, tmp_path):
    us_industry.companies_with_sic(3571, SnapshotStore(tmp_path))
    starts = [p.get("start") for u, p in fake_sec if p.get("SIC") == 3571]
    assert starts == [0]                                      # 首页不满一页就停


def _browse_calls(calls):
    return [p["SIC"] for u, p in calls if "browse-edgar" in u]


def test_只请求SEC实际使用的SIC码(fake_sec, tmp_path):
    us_industry.peer_set_us("AAPL", SnapshotStore(tmp_path))
    # FF49-35 的区间展开有 21 个码，SEC 码表里只有 7 个
    assert sorted(_browse_calls(fake_sec)) == [3570, 3571, 3572, 3575, 3576, 3577, 3578, 3579]


def test_SIC码表抓不到时退回全区间(fake_sec, tmp_path):
    SIC_LIST_BROKEN["on"] = True
    us_industry.peer_set_us("AAPL", SnapshotStore(tmp_path))
    assert len(_browse_calls(fake_sec)) == len(us_industry.sic_codes_of(35))


def test_按SIC的公司名单有本地缓存(fake_sec, tmp_path):
    store = SnapshotStore(tmp_path)
    us_industry.peer_set_us("AAPL", store)
    first = len(_browse_calls(fake_sec))
    us_industry.peer_set_us("AAPL", store)
    assert first > 0 and len(_browse_calls(fake_sec)) == first        # 第二次不再请求


def test_缓存过期后重新请求(fake_sec, tmp_path, monkeypatch):
    store = SnapshotStore(tmp_path)
    us_industry.companies_with_sic(3571, store)
    monkeypatch.setattr(us_industry, "CACHE_TTL_DAYS", -1)
    us_industry.companies_with_sic(3571, store)
    assert _browse_calls(fake_sec) == [3571, 3571]
