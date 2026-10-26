"""第二次调用 ② --brief 的美股适配（用户 2026-10-26 批准的九项）。架构不变；A 股提示词逐字不变。不联网。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from ir_agent.drivers import Factor
from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.sources.external import Corpus, Document, Tier

AS_OF = date(2026, 10, 26)


# ── 1 / 3：方法论预研提示词 ─────────────────────────────────────────

def test_美股预研提示_营收单位百万美元_不提卖方研报_检索范围写10K():
    from ir_agent import methodology
    p = methodology.build_prompt("FF49-35", "AAPL")
    assert "营收的单位是百万美元" in p and "亿元" not in p
    assert "卖方研报" not in p and "研报价格跟踪" not in p
    assert "10-K" in p and "8-K" in p and "年报/公告" not in p


def test_A股预研提示逐字不变():
    from ir_agent import methodology
    p = methodology.build_prompt("电子", "600183")
    assert p == methodology.PROMPT.format(industry="电子", code="600183", market="A 股", context="")


# ── 2：方法论审核的第 0 年试算按币种换算 ─────────────────────────────

def test_试算换算按币种_美股百万美元_A股亿元():
    from ir_agent.methodology_review import revenue_scale
    us, cn = FactLedger(), FactLedger()
    us.put(Fact(key="revenue", value=D("416161000000"), unit="美元", currency="USD", period="2025FY",
                as_of=AS_OF, source_id="s", method=Method.REPORTED))
    cn.put(Fact(key="revenue", value=D("20000000000"), unit="元", currency="CNY", period="2025FY",
                as_of=AS_OF, source_id="s", method=Method.REPORTED))
    assert revenue_scale(us) == (D(10) ** 6, "百万美元")
    assert revenue_scale(cn) == (D(10) ** 8, "亿元")
    assert revenue_scale(None) == (D(10) ** 8, "亿元")


# ── 4：④ 级联网检索提示 ────────────────────────────────────────────

@pytest.fixture
def web_on(monkeypatch):
    from ir_agent.sources import web
    monkeypatch.setattr(web, "WEB_CAPABLE", True)


def _captured(fn, **kw):
    seen = []
    fn(lambda p: seen.append(p) or "", "平均售价变化率", "f_asp", "FF49-35", AS_OF, **kw)
    return seen[0]


def test_美股因子检索提示_两个入口都分市场(web_on):
    from ir_agent.sources import conclusions, web
    for fn in (web.search, conclusions.search):
        p = _captured(fn, us=True)
        assert "美股" in p and "A 股" not in p
        assert "Census" in p and "Business Wire" in p and "sec.gov" in p and "PDF" in p


def test_A股因子检索提示逐字不变(web_on):
    from ir_agent.sources import conclusions, web
    for fn, mod in ((web.search, web), (conclusions.search, conclusions)):
        assert _captured(fn) == mod.PROMPT.format(industry="FF49-35", factor="平均售价变化率",
                                                  key="f_asp", as_of=AS_OF.isoformat())


# ── 5：简报写作要求的引述示例 ──────────────────────────────────────

def test_美股简报引述示例写10K与机构_A股不变():
    from ir_agent.analyst import FactCatalog, build_prompt
    cat = FactCatalog(entries=[], ledger=FactLedger(), as_of=AS_OF)
    us = build_prompt(cat, "AAPL", "2025FY", brief=True)
    cn = build_prompt(cat, "600183", "2025FY", brief=True)
    assert "某某证券研报指出" not in us and "据 10-K 披露" in us
    assert "某某证券研报指出" in cn


# ── 6 / 7：②.5 只看本司文档；同业 10-K 不作因子文字证据 ─────────────

def _doc(doc_id, text, channel="", tier=Tier.COMPANY, publisher="Apple Inc."):
    return Document(doc_id=doc_id, url=f"https://x/{doc_id}", title=doc_id, publisher=publisher, tier=tier,
                    published=date(2026, 7, 30), retrieved=AS_OF, text=text, channel=channel)


def _factor():
    return Factor(key="asp", label="平均售价", unit="变化率")


def test_公司公告级不命中同业10K():
    from ir_agent import gaps
    c = Corpus(as_of=AS_OF)
    c.add(_doc("peer10k_DELL", "Our 平均售价 rose sharply this year.", channel="peer_filing", publisher="Dell"))
    assert gaps._match_announce(c, "平均售价") is None
    assert gaps._match_report(c, "平均售价") is None              # ③ 也不收同业自述


def test_命中电话会纪要时注明二手转存(monkeypatch):
    from ir_agent import gaps
    from ir_agent.drivers import Decomposition, Op
    c = Corpus(as_of=AS_OF)
    c.add(_doc("defeatbeta_AAPL", "Tim: 平均售价 was up year over year.", channel="transcript",
               publisher="defeatbeta（二手）"))
    d = Decomposition(target="revenue", op=Op.MULTIPLY, factors=[_factor()])
    out = gaps.collect(None, [d], FactLedger(), c, "x", AS_OF)
    fd = next(iter(out.values()))
    assert fd.status == gaps.ANNOUNCE and "电话会纪要" in fd.note and "二手" in fd.note


# ── 8：美股状态显示 ────────────────────────────────────────────────

def test_美股摘要把研报测算显示为机构测算_年报披露显示为10K披露():
    from ir_agent import gaps
    rows = {"a": gaps.FactorData(factor=_factor(), status=gaps.REPORT, note="IDC"),
            "b": gaps.FactorData(factor=Factor(key="b", label="服务占比", unit="pp"),
                                 status=gaps.ANNUAL, note="10-K")}
    us, cn = gaps.summary(rows, us=True), gaps.summary(rows)
    assert "机构测算" in us and "研报测算" not in us and "10-K 披露" in us and "年报披露" not in us
    assert "研报测算" in cn and "年报披露" in cn


# ── 9：机构名白名单补美国政府统计机构 ────────────────────────────────

@pytest.mark.parametrize("name", ["U.S. Census Bureau", "Bureau of Labor Statistics", "BLS",
                                  "Bureau of Economic Analysis", "U.S. Energy Information Administration",
                                  "Federal Reserve", "Federal Reserve Board", "USTR",
                                  "U.S. Department of Commerce", "Federal Register"])
def test_美国政府统计机构判为官方级(name):
    from ir_agent.sources.web import tier_of
    assert tier_of(name) is Tier.OFFICIAL


@pytest.mark.parametrize("name", ["Census Watch", "BLS Daily", "Federal Reserve Blog Times"])
def test_仿冒名仍落媒体级(name):
    from ir_agent.sources.web import tier_of
    assert tier_of(name) is Tier.MEDIA
