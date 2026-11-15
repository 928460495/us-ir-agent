"""「通讯社」来源等级（用户 2026-11-15 决定）：已发生事实经审核可作依据。不联网。"""

from __future__ import annotations

from datetime import date

import pytest

from ir_agent.sources.external import TIER_LABEL, TIER_RULE, Corpus, Document, Tier
from ir_agent.sources.openweb import tier_of_url
from ir_agent.sources.web import tier_of


@pytest.mark.parametrize("url", ["https://www.reuters.com/business/x", "https://apnews.com/article/y",
                                 "https://www.afp.com/en/news/z", "https://www.bloomberg.com/news/a",
                                 "https://www.wsj.com/b", "https://www.ft.com/content/c", "https://www.cnbc.com/d"])
def test_通讯社网址判NEWSWIRE(url):
    assert tier_of_url(url) is Tier.NEWSWIRE


@pytest.mark.parametrize("url", ["https://reuters.com.evil.io/x", "https://notreuters.com/y",
                                 "https://www.cnbc.com.cn.fake/z"])
def test_仿冒通讯社域名判MEDIA(url):
    assert tier_of_url(url) is Tier.MEDIA


@pytest.mark.parametrize("name", ["Reuters", "Thomson Reuters", "Associated Press", "The Associated Press",
                                  "Agence France-Presse", "AFP", "Bloomberg", "Bloomberg News",
                                  "The Wall Street Journal", "Wall Street Journal", "Financial Times", "CNBC"])
def test_通讯社机构名判NEWSWIRE(name):
    assert tier_of(name) is Tier.NEWSWIRE


@pytest.mark.parametrize("name", ["Reuters Blog Watch", "CNBC Daily Bites", "AP", "FT", "Bloomberg Opinion Weekly"])
def test_通讯社名加媒体后缀或两字母缩写仍判MEDIA(name):
    assert tier_of(name) is Tier.MEDIA


def test_通讯社可引用范围写明已发生事实与不得作依据的几类():
    assert TIER_LABEL[Tier.NEWSWIRE] == "通讯社"
    rule = TIER_RULE[Tier.NEWSWIRE]
    assert "已发生事实" in rule and "审核者" in rule and "匿名" in rule and "预测" in rule


def test_来源清单通讯社单独成节():
    from ir_agent.ledger import FactLedger
    from ir_agent.sources.manifest import collect_rows, render
    c = Corpus(as_of=date(2026, 11, 15))
    c.add(Document(doc_id="ow_r", url="https://www.reuters.com/x", title="Palantir wins Army deal",
                   publisher="Reuters", tier=Tier.NEWSWIRE, published=date(2026, 10, 1),
                   retrieved=date(2026, 11, 15), text="x"))
    rows = collect_rows(FactLedger(), corpus=c)
    assert [r.kind for r in rows if r.origin == "https://www.reuters.com/x"] == ["通讯社"]
    assert "## 通讯社（1 项）" in render(rows, [])


def test_白名单跨档互不为前缀_含通讯社():
    from ir_agent.sources import web
    tiers = {Tier.SELLSIDE: web._SELLSIDE_NORM, Tier.OFFICIAL: web._OFFICIAL_NORM,
             Tier.NEWSWIRE: web._NEWSWIRE_NORM}
    for t1, xs in tiers.items():
        for t2, ys in tiers.items():
            if t1 is t2:
                continue
            assert not [(a, b) for a in xs for b in ys if a and b and b.startswith(a)], (t1, t2)
