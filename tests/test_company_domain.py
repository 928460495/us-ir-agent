"""公司官网页面判为公司披露：域名取自 10-K「Available Information」一句（SEC submissions 的
website 字段实测为空）。不联网。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from ir_agent.sources.external import Corpus, Tier
from ir_agent.sources.tenk import company_domain

AS_OF = date(2026, 11, 15)


def test_从可获取信息一节取官网域名():
    t = "Available Information. Our website address is www.palantir.com. Information on our website is not part of"
    assert company_domain(t) == "palantir.com"
    assert company_domain("Our Internet address is https://investor.apple.com/ and our filings") == "apple.com"


def test_不把SEC与第三方网址当官网():
    t = ("The SEC maintains an Internet website that contains reports at www.sec.gov. "
         "Our corporate website is www.foo.com.")
    assert company_domain(t) == "foo.com"
    assert company_domain("no web address mentioned here at all") is None


def _gather(monkeypatch, company_hosts):
    from ir_agent.sources import openweb

    def fake_fetch(url, **k):
        return openweb.Fetched(final_url=url, text="Palantir announced a new AIP release.",
                               meta_published=date(2026, 10, 1))
    monkeypatch.setattr(openweb, "fetch", fake_fetch)
    line = "候选: 公司重大动态 | AIP 发布 | https://www.palantir.com/newsroom/aip | Palantir | 2026-10-01"
    corpus = Corpus(as_of=AS_OF)
    openweb.gather(lambda p: line, code="PLTR", company="Palantir", industry="x", as_of=AS_OF,
                   corpus=corpus, company_hosts=company_hosts)
    return corpus.docs[0]


def test_公司自有域名下的页面判公司披露(monkeypatch):
    assert _gather(monkeypatch, ("palantir.com",)).tier is Tier.COMPANY


def test_没有官网域名时照旧按网址分级(monkeypatch):
    assert _gather(monkeypatch, ()).tier is Tier.MEDIA


def test_10K取不到时业务摘要为空且不抛(monkeypatch):
    from ir_agent import full
    monkeypatch.setattr(full, "_load_sections", lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    assert full._us_business(SimpleNamespace()) == ("", None)


def test_业务摘要截取800字并带出官网域名(monkeypatch):
    from ir_agent import full
    body = "Palantir builds software. " * 60 + "Our website address is www.palantir.com."
    sec = SimpleNamespace(get=lambda k: SimpleNamespace(text=body) if k == "business" else None)
    monkeypatch.setattr(full, "_load_sections", lambda r: (sec, ""))
    text, dom = full._us_business(SimpleNamespace())
    assert len(text) == 800 and text.startswith("Palantir builds software") and dom == "palantir.com"
