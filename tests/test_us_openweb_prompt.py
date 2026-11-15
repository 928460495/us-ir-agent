"""美股开放网络检索：全行业通用主题、业务摘要、监管机构、每主题 3 篇合计 20 篇（用户 2026-11-15 决定）。
A 股提示、槽位、数量逐字不变。不联网。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from ir_agent.sources import openweb
from ir_agent.sources.external import Corpus
from ir_agent.sources.openweb import US_SLOTS
from ir_agent.sources.us_regulators import for_ff49

AS_OF = date(2026, 11, 15)


def _prompt(code, **kw):
    seen = []

    def client(p):
        seen.append(p)
        return ""
    openweb.gather(client, code=code, company="X", industry="Y", as_of=AS_OF,
                   corpus=Corpus(as_of=AS_OF), **kw)
    return seen[0]


def test_美股提示六个通用主题_不再有制造业措辞():
    p = _prompt("PLTR")
    for t in US_SLOTS:
        assert f"- {t}" in p
    assert "稼动率" not in p and "上下游价格与供需" not in p and "最多 3 个" in p


def test_美股提示带业务摘要与监管机构_并要求避开付费墙():
    p = _prompt("PLTR", business="Palantir builds software platforms for analytics.",
                regulators="FTC（ftc.gov）")
    assert "Palantir builds software" in p and "ftc.gov" in p and "付费" in p and "登录" in p
    # 优先级：官方 → 协会与一手机构 → 通讯社 → 其他媒体
    assert p.index("行业协会") < p.index("通讯社") < p.index("其他媒体")


def test_美股提示业务与监管为空时整段省略():
    p = _prompt("PLTR")
    assert "公司业务" not in p and "监管机构官网" not in p


def _lines(themes, n):
    return "\n".join(f"候选: {t} | 标题{t}{i} | https://ex{abs(hash(t)) % 10**6}.com/{i} | Pub | 2026-10-01"
                     for t in themes for i in range(n))


def _gather_count(monkeypatch, raw):
    monkeypatch.setattr(openweb, "fetch", lambda url, **k: openweb.Fetched(
        final_url=url, text="body " + url, meta_published=date(2026, 10, 1)))
    res = openweb.gather(lambda p: raw, code="PLTR", company="X", industry="Y", as_of=AS_OF,
                         corpus=Corpus(as_of=AS_OF))
    return res


def test_美股数量上限二十篇_每主题三篇(monkeypatch):
    res = _gather_count(monkeypatch, _lines(US_SLOTS, 5))
    assert len(res.docs) == 18 and res.missing_slots == []
    extra = [f"追加{i}" for i in range(7)]
    res = _gather_count(monkeypatch, _lines(US_SLOTS, 5) + "\n" + _lines(extra, 2))
    assert len(res.docs) == 20


def test_A股提示与数量逐字不变():
    assert _prompt("600183") == openweb.PROMPT.format(company="X", code="600183", industry="Y",
                                                       per_slot=openweb.PER_SLOT, as_of=AS_OF.isoformat())
    assert openweb.slots_for("600183") == openweb.SLOTS and openweb.PER_SLOT == 2 and openweb.MAX_DOCS == 10
    assert openweb.slots_for("PLTR") == US_SLOTS and openweb.PER_SLOT_US == 3 and openweb.MAX_DOCS_US == 20


def test_美股缺口按美股槽位判定(monkeypatch):
    res = _gather_count(monkeypatch, "")
    assert res.missing_slots == list(US_SLOTS)


def test_读回时美股一篇没采到按美股槽位判定(tmp_path):
    from ir_agent import full
    openweb.save(openweb.OpenWebResult(missing_slots=list(US_SLOTS)), tmp_path / openweb.OPENWEB_JSON)
    r = SimpleNamespace(code="PLTR", as_of=AS_OF, corpus=None)
    notes = full._restore_openweb(r, tmp_path)
    assert "第一阶段确实一篇都没采到" in notes[0]


def test_监管机构按FF49大类():
    assert ("FDA", "fda.gov") in for_ff49(13) and ("FTC", "ftc.gov") in for_ff49(36)
    assert ("DOJ", "justice.gov") in for_ff49(1)
    assert ("FDIC", "fdic.gov") in for_ff49(45) and not any(d == "sec.gov" for _, d in for_ff49(48))
    assert ("FAA", "faa.gov") in for_ff49(24) and ("FERC", "ferc.gov") in for_ff49(31)
    assert ("FCC", "fcc.gov") in for_ff49(32) and any(d == "defense.gov" for _, d in for_ff49(26))
    assert any(d == "bis.gov" for _, d in for_ff49(36))


def test_load_openweb美股分支传入业务摘要_监管机构与官网域名(monkeypatch, tmp_path):
    from ir_agent import full
    got = {}

    def fake_gather(client, **kw):
        got.update(kw)
        return openweb.OpenWebResult(missing_slots=list(US_SLOTS))
    monkeypatch.setattr(openweb, "gather", fake_gather)
    monkeypatch.setattr(full, "_us_business", lambda r: ("Palantir builds software.", "palantir.com"))
    monkeypatch.setattr(full, "_company_name", lambda c: "Palantir")
    r = SimpleNamespace(corpus=None, peer_set=SimpleNamespace(
        classification=SimpleNamespace(used_path="FF49-36 Computer Software")))
    full._load_openweb("PLTR", r, AS_OF, tmp_path, object(), "FF49-36")
    assert got["business"] == "Palantir builds software."
    assert "FTC（ftc.gov）" in got["regulators"] and got["company_hosts"] == ("palantir.com",)


def test_因子检索提示加通讯社与付费墙():
    from ir_agent.sources import conclusions, web
    for p in (web.PROMPT_US, conclusions.PROMPT_US):
        assert "Reuters" in p and "付费" in p
