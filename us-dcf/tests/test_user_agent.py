import re
from pathlib import Path

import pytest

import us_data


def test_没有环境变量时拒绝发请求(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        us_data.sec_headers()


def test_有环境变量时用它作身份(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Tester tester@example.org")
    assert us_data.sec_headers() == {"User-Agent": "Tester tester@example.org"}


def test_代码里不出现邮箱():
    src = Path(us_data.__file__).read_text(encoding="utf-8")
    assert not re.search(r"[\w.]+@[\w.]+\.\w+", src)


def test_SEC请求带身份_其他站点不带(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Tester tester@example.org")
    seen = {}

    def fake_urlopen(req, timeout):
        seen[req.full_url] = req.get_header("User-agent")

        class R:
            def read(self):
                return b"{}"
        return R()

    monkeypatch.setattr(us_data.urllib.request, "urlopen", fake_urlopen)
    us_data._get("https://data.sec.gov/submissions/CIK0000320193.json")
    us_data._get("https://financialmodelingprep.com/stable/quote?symbol=AAPL")
    assert seen["https://data.sec.gov/submissions/CIK0000320193.json"] == "Tester tester@example.org"
    assert "tester@example.org" not in seen["https://financialmodelingprep.com/stable/quote?symbol=AAPL"]


def test_缺身份时SEC请求立即报错且不重试(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    called = []
    monkeypatch.setattr(us_data.urllib.request, "urlopen", lambda *a, **k: called.append(1))
    monkeypatch.setattr(us_data.time, "sleep", lambda s: called.append("sleep"))
    with pytest.raises(RuntimeError, match="export SEC_USER_AGENT"):
        us_data._get("https://data.sec.gov/submissions/CIK0000320193.json")
    assert called == []
