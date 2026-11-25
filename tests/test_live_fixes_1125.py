"""NVDA 美股实跑（2026-11-25）发现的问题。不联网。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ir_agent.ledger import Fact, FactLedger, Method
from ir_agent.sources import conclusions as C
from ir_agent.sources.external import Corpus, Document, Tier
from ir_agent.sources.openweb import Fetched

D = Decimal
AS_OF = date(2026, 11, 25)


# ── 1 英文页面不再被当成打不开 ─────────────────────────────────

EN = ("Nominal 10-year TIPS yields rose to 2.79 percent on November 23, the Treasury said, as investors "
      "priced in further policy tightening by the Federal Reserve over the coming months.")


def _open(monkeypatch, text, claim="实际收益率 2.79%"):
    monkeypatch.setattr(C, "fetch", lambda u, **k: Fetched(final_url=u, text=text, meta_published=None))
    c = C.Conclusion(factor_key="k", claim=claim, articles=[C.Article(
        no="1", url="https://home.treasury.gov/x", publisher="UST", published=AS_OF)])
    C.open_articles([c])
    return c


def test_英文正文算打开并能核对数字(monkeypatch):
    c = _open(monkeypatch, EN)
    assert c.articles[0].opened
    assert c.marks[0].found_in == ("1",)


@pytest.mark.parametrize("text", [
    "Just a moment... Enable JavaScript and cookies to continue. Checking your browser before accessing the site. "
    "This process is automatic. Your browser will redirect to your requested content shortly. Please allow up to five seconds.",
    "Access Denied You don't have permission to access this page on this server. Reference #18.5f3 Please contact the "
    "site owner if you believe this is an error and include the reference number shown above in your message.",
    "Please verify you are a human. Press and hold the button below to confirm you are a human and not a bot before "
    "continuing to the page that you have requested from this web site today.",
    "x9d8f7 " + "QmFzZTY0" * 200,
    "",
])
def test_反爬页与无正文仍判打不开(monkeypatch, text):
    a = _open(monkeypatch, text).articles[0]
    assert not a.opened and "无从比对" in a.why_not


def test_长正文里偶然出现反爬措辞不误伤(monkeypatch):
    a = _open(monkeypatch, EN * 20 + " The bank said access denied errors had risen.").articles[0]
    assert a.opened


# ── 3 净负债率计入短期投资 ────────────────────────────────────

def _f(key, v, period="2026FY", unit="美元"):
    return Fact(key=key, value=D(str(v)), unit=unit, period=period, as_of=date(2026, 2, 25), source_id="x",
                method=Method.REPORTED)


def test_净负债率减去短期投资():
    from ir_agent.industry_profile import company_exposure_facts
    led = FactLedger()
    for k, v in (("short_loan", 10), ("long_loan", 20), ("cash_and_equivalents", 30),
                 ("trading_finasset", 100), ("equity_attr_parent", 400)):
        led.put(_f(k, v))
    got = {f.key: f.value for f in company_exposure_facts(led, "2026FY", AS_OF)}
    assert got["company.net_debt_ratio"] == D("-0.25")            # (30 − 30 − 100) ÷ 400


# ── 6 审核引文核对：稿子原句、省略号 ───────────────────────────

def _note(why, hay="", draft=""):
    from ir_agent import methodology_review as R
    v = R.Verdict("护城河", "第1条", "提醒", why, suggest="")
    return R.annotate_quotes([v], hay=hay, ledger=FactLedger(), as_of=AS_OF, draft=draft)[0].quote_note


def test_引稿子自己的话不算找不到():
    assert _note("「承担上游涨价的风险」没有引文", draft="本公司同时也承担上游涨价的风险。") == ""


def test_引稿子时跨占位符也能对上():
    assert _note("「期限利差为正，是短端被推高」", draft="期限利差 [[macro.term_spread@2026-11-24]] 为正，是短端被推高后") == ""


def test_省略号截短的引文按片段核对():
    hay = "The large and growing number of developers and installed base across our platforms strengthens"
    assert _note("「The large and growing number of developers…」", hay=hay) == ""
    assert _note("「The large and growing number … strengthens」", hay=hay) == ""


def test_编造的引文仍标找不到():
    assert "找不到" in _note("「公司已与客户签订十年长约」", hay="别的内容", draft="别的稿子")
    assert "找不到" in _note("「The large and growing … fabricated ending」", hay="The large and growing number")


def test_省略号片段必须按原文顺序():
    assert "找不到" in _note("「strengthens our ecosystem … The large and growing」",
                          hay="The large and growing number strengthens our ecosystem")


# ── 7 复述账本合计时不列「未生成（）」 ─────────────────────────

def test_复述账本的规模行不记未生成(monkeypatch):
    from ir_agent import full, value_chain as VC
    view = VC.ChainView()
    view.size = SimpleNamespace(bases=[SimpleNamespace(kind=__import__(
        "ir_agent.basis", fromlist=["BasisKind"]).BasisKind.FACT)])
    monkeypatch.setattr(VC, "analyse", lambda *a, **k: (view, ""))
    r = SimpleNamespace(corpus=Corpus(as_of=AS_OF), sections=None, ledger=FactLedger(), period="2026FY", store=None)
    monkeypatch.setattr(VC.ChainView, "summary", lambda self: "s")
    assert full._load_value_chain(r, AS_OF, object())[2] == ""


# ── 4 业绩已发布、定期报告未申报的时点提示 ─────────────────────

def _sub(rows):
    cols = {k: [] for k in ("form", "filingDate", "items", "reportDate")}
    for form, filed, items, rep in rows:
        cols["form"].append(form); cols["filingDate"].append(filed)
        cols["items"].append(items); cols["reportDate"].append(rep)
    return {"filings": {"recent": cols}}


def test_业绩8K晚于最新定期报告时提示():
    from ir_agent.sources.edgar_filings import pending_report
    n = pending_report(_sub([("8-K", "2026-11-17", "2.02,9.01", "2026-11-17"),
                             ("10-Q", "2026-08-26", "", "2026-07-26")]), AS_OF)
    assert "2026-11-17" in n and "2026-07-26" in n
    from ir_agent.citation import audit_bare_numbers
    assert audit_bare_numbers(n) == []                    # 会进撰写提示词，不能带写不进正文的裸数字


@pytest.mark.parametrize("rows", [
    [("10-Q", "2026-11-17", "", "2026-10-25"), ("8-K", "2026-11-17", "2.02", "2026-11-17")],   # 同日申报
    [("8-K", "2026-11-30", "2.02", "2026-11-30"), ("10-Q", "2026-08-26", "", "2026-07-26")],   # 时点之后
    [("8-K", "2026-09-05", "2.02", "2026-09-05"), ("10-Q", "2026-08-26", "", "2026-07-26")],   # 十天后补充披露，不是新一季
    [("8-K", "2026-11-17", "5.02", "2026-11-17"), ("10-Q", "2026-08-26", "", "2026-07-26")],   # 不是业绩
])
def test_其余情形不提示(rows):
    from ir_agent.sources.edgar_filings import pending_report
    assert pending_report(_sub(rows), AS_OF) == ""


def test_时点提示进账本目录与来源清单():
    from ir_agent import full
    from ir_agent.sources import manifest
    r = SimpleNamespace(timing_note="业绩新闻稿已发布而定期报告未申报", ledger=FactLedger(), period="2026FY")
    assert "定期报告未申报" in full._qual_catalog(r, AS_OF)
    assert "定期报告未申报" in manifest.render([], [], timing="业绩新闻稿已发布而定期报告未申报")


# ── 5 战略稿逐条回应重大公告 ─────────────────────────────────

def _doc(title, when, text, rank=2, url=None, channel=""):
    return Document(doc_id=title, url=url or f"https://x/{title}", title=title, publisher="NVIDIA CORP",
                    tier=Tier.COMPANY, published=when, retrieved=AS_OF, text=text, prompt_limit=8000,
                    rank=rank, channel=channel)


def _corpus(*docs):
    c = Corpus(as_of=AS_OF)
    for d in docs:
        c.add(d)
    return c


CEO = "8-K 高管或董事变动（2026-11-12）"
COVER = "UNITED STATES SECURITIES AND EXCHANGE COMMISSION cover page boilerplate. " * 30


def _us_corpus():
    return _corpus(
        _doc(CEO, date(2026, 11, 12), COVER + "Item 5.02. Departure of Directors. The Board accepted the resignation."),
        _doc("8-K 业绩发布（2026-11-17）", date(2026, 11, 17), "Revenue", rank=1),
        _doc("8-K 业绩附件 EX-99.2（2026-11-17）", date(2026, 11, 17), "Commentary"),
        _doc("8-K 重大协议（2026-08-17）", date(2026, 8, 17), "Item 1.01 guarantee", rank=1),
        _doc("8-K 高管或董事变动（2026-01-23）", date(2026, 1, 23), "Item 5.02 old"),
        _doc("FY2027 Q3 业绩电话会纪要", date(2026, 11, 17), "call", channel="transcript"))


def test_重大公告清单_美股排除业绩与超窗与纪要():
    from ir_agent import strategy
    assert [d.title for d in strategy.material_events(_us_corpus())] == [CEO, "8-K 重大协议（2026-08-17）"]


def test_重大公告清单_A股取第一档且同类月报只留最新():
    from ir_agent import strategy
    c = _corpus(_doc("2026年10月份生猪销售简报", date(2026, 11, 6), "a", rank=1),
                _doc("2026年9月份生猪销售简报", date(2026, 10, 8), "b", rank=1),
                _doc("关于投资建设覆铜板项目的公告", date(2026, 9, 1), "c", rank=1),
                _doc("关于担保的公告", date(2026, 10, 1), "d", rank=2))
    assert [d.title for d in strategy.material_events(c)] == ["2026年10月份生猪销售简报", "关于投资建设覆铜板项目的公告"]


def test_提示词列出公告且正文从条目处起():
    from ir_agent import strategy
    txt = strategy.events_block(_us_corpus())
    assert CEO in txt and "accepted the resignation" in txt and "cover page boilerplate" not in txt
    assert strategy.events_block(_corpus()) == ""


def test_没回应的公告交审核者并进检查单():
    from ir_agent import strategy
    c = _us_corpus()
    v = strategy.parse(f"公告: {CEO} | 采用 | 管理层稳定性存疑，影响不确定性判断", c)
    fs = strategy.facts(v, FactLedger(), "", AS_OF, c)
    assert any("没有回应" in x and "8-K 重大协议（2026-08-17）" in x and CEO not in x for x in fs)
    ev = [s for k, s in strategy.checklist(v) if k == "公告"]
    assert len(ev) == 2 and len(set(ev)) == 2
    assert "公告" in strategy.KINDS


def test_不采用须写理由_处理方式须是规定的词():
    from ir_agent import strategy
    c = _us_corpus()
    v = strategy.parse(f"公告: {CEO} | 不采用 |\n公告: 8-K 重大协议（2026-08-17） | 忽略 | 无关", c)
    notes = "；".join(v.read_notes + strategy.facts(v, FactLedger(), "", AS_OF, c))
    assert "没写" in notes and "忽略" in notes


def test_没有重大公告时检查单与摘要不变():
    from ir_agent import strategy
    v = strategy.parse("护城河: 无形资产 | 宽 | 「x」", _corpus())
    assert not [1 for k, _ in strategy.checklist(v) if k == "公告"]
    assert "重大公告" not in v.summary()


def test_回应进摘要且不带账本外数字():
    from ir_agent import strategy
    from ir_agent.citation import audit_bare_numbers
    c = _us_corpus()
    v = strategy.parse(f"公告: {CEO} | 采用 | 管理层稳定性存疑\n公告: 8-K 重大协议（2026-08-17） | 不采用 | 与估值无关", c)
    s = v.summary()
    assert "高管或董事变动" in s and "管理层稳定性存疑" in s and "与估值无关" in s
    assert audit_bare_numbers("\n".join(x for x in s.splitlines() if "公告" in x or "变动" in x)) == []


# ── 1（续）有结论没出处的跟踪指标，重跑时再搜 ─────────────────

def test_无出处的跟踪指标重跑时再检索(monkeypatch):
    from ir_agent import macro_track as MT, macro_view as MV
    calls = []

    def fake(client, label, key, industry, as_of, us=False):
        calls.append(label)
        return C.Searched(items=[], dropped=[])
    monkeypatch.setattr(C, "search", fake)
    view = SimpleNamespace(tracks=[SimpleNamespace(indicator="甲", driver="d", falsifier="f"),
                                   SimpleNamespace(indicator="乙", driver="d", falsifier="f")])
    done = MT.TrackResult(items=[MT.TrackItem("甲", "d", "f", "有结论", []),
                                 MT.TrackItem("乙", "d", "f", "有结论", ["https://x"])])
    MT.track(object(), view, industry="x", as_of=AS_OF, corpus=None, us=True, done=done)
    assert calls == ["甲"]


# ── 终审（2026-11-25）──────────────────────────────────────────

def test_I1_大写ITEM与目录不误切():
    from ir_agent import strategy
    amzn = _corpus(_doc("8-K 其他事项（2026-10-01）", date(2026, 10, 1),
                        COVER + "TABLE OF CONTENTS ITEM 8.01. OTHER EVENTS. 3 ITEM 9.01. EXHIBITS 3 SIGNATURES 4 "
                        + "ITEM 8.01. OTHER EVENTS. " + "The company agreed to settle. " * 10 + "ITEM 9.01. EXHIBITS. none"))
    txt = strategy.events_block(amzn)
    assert "agreed to settle" in txt and "cover page boilerplate" not in txt and "TABLE OF CONTENTS" not in txt
    long_toc = _corpus(_doc("8-K 高管或董事变动（2026-09-09）", date(2026, 9, 9),
                            COVER + "TABLE OF CONTENTS ITEM 5.02. DEPARTURE OF DIRECTORS OR CERTAIN OFFICERS; ELECTION OF "
                            "DIRECTORS; APPOINTMENT OF CERTAIN OFFICERS 3 ITEM 9.01. EXHIBITS. 4 SIGNATURES 5 EXHIBIT 1.1 "
                            + "EXHIBIT 4.1 EXHIBIT 4.2 EXHIBIT 4.3 EXHIBIT 4.4 " * 3
                            + "ITEM 5.02. DEPARTURE. The director resigned, see Item 9.01 below. ITEM 9.01. EXHIBITS. x"))
    txt = strategy.events_block(long_toc)
    assert "director resigned" in txt and "EXHIBIT 4.1" not in txt
    ref = _corpus(_doc("8-K 重大协议（2026-06-10）", date(2026, 6, 10),
                       COVER + "Item 1.01 Entry into an Agreement. " + "The company signed a lease. " * 8
                       + "Item 2.03 Obligation. The information in Item 1.01 above is incorporated."))
    assert "signed a lease" in strategy.events_block(ref)
    t = _corpus(_doc("8-K 高管或董事变动（2026-10-16）", date(2026, 10, 16),
                     COVER + "ITEM 5.02 Departure. " + "The CFO will retire in March. " * 8 + "Item 9.01 Exhibits. x"))
    assert "CFO will retire" in strategy.events_block(t)


@pytest.mark.parametrize("a,b", [("关于对外投资的公告", "关于对外投资的公告"),
                                 ("关于签订重大合同的公告（一）", "关于签订重大合同的公告（二）")])
def test_I3_通用标题的不同公告不被去重(a, b):
    from ir_agent import strategy
    c = _corpus(_doc(a, date(2026, 10, 1), "x", rank=1, url="https://x/1"), _doc(b, date(2026, 8, 1), "y", rank=1, url="https://x/2"))
    assert len(strategy.material_events(c)) == 2


@pytest.mark.parametrize("head", [
    "第1条 中国巨石关于巨石集团有限公司与巨石集团香港有限公司共同增资的公告",     # 补全了截断的标题
    "第1条 《中国巨石关于巨石集团有限公司与巨石集团香港有限公司》",
    "第1条",
    "第1条 中国巨石关于（半角）(2026-11-12) 的公告：补充",
])
def test_I5_公告条目按序号认(head):
    from ir_agent import methodology_review as R
    exp = [("公告", "第1条 中国巨石关于巨石集团有限公司与巨石集团香港有限公"), ("公告", "第2条 另一条公告"), ("自洽", "x")]
    vs = R.parse(f"[公告] {head}：提醒　理由　建议：补\n[公告] 第2条 另一条公告：通过\n[自洽] x：通过", exp,
                 kinds=("公告", "自洽"))
    assert [v.level for v in vs] == ["提醒", "通过", "通过"]


def test_I5_撰写者标题全半角与竖线():
    from ir_agent import strategy
    piped = "8-K 其他事项 A|B（2026-10-01）"
    c = _corpus(_doc(CEO, date(2026, 11, 12), "Item 5.02 x"), _doc(piped, date(2026, 10, 1), "Item 8.01 y"))
    v = strategy.parse(f"公告: 8-K 高管或董事变动 (2026-11-12) | 采用 | 影响一\n公告: {piped} | 不采用 | 理由二", c)
    assert [(e.title, e.decision, e.note) for e in v.events] == [(CEO, "采用", "影响一"), (piped, "不采用", "理由二")]


def test_M6_正文里的reCAPTCHA页脚不误伤(monkeypatch):
    assert _open(monkeypatch, EN + " This site is protected by reCAPTCHA and the Google Privacy Policy.").articles[0].opened


def test_M7_省略号片段太短或相距太远不算():
    far = "revenue grew. " + "filler words here. " * 200 + "decline in guidance"
    assert "找不到" in _note("「revenue grew … decline in guidance」", hay=far)
    assert "找不到" in _note("「公司…毛利率…下降」", hay="公司本年毛利率有所下降")


def test_M9_重搜失败时保留上轮结论(monkeypatch):
    from ir_agent import macro_track as MT

    def boom(*a, **k):
        raise RuntimeError("超时")
    monkeypatch.setattr(C, "search", boom)
    view = SimpleNamespace(tracks=[SimpleNamespace(indicator="甲", driver="d", falsifier="f")])
    res = MT.track(object(), view, industry="x", as_of=AS_OF, corpus=None, us=True,
                   done=MT.TrackResult(items=[MT.TrackItem("甲", "d", "f", "上轮结论", [])]))
    assert res.items[0].conclusion == "上轮结论" and res.failed


def test_M8_三月底申报年报后的一季度业绩也提示():
    from ir_agent.sources.edgar_filings import pending_report
    n = pending_report(_sub([("8-K", "2026-04-24", "2.02", "2026-04-24"),
                             ("10-K", "2026-03-28", "", "2025-12-31")]), AS_OF)
    assert "2026-04-24" in n and "初步" in n
