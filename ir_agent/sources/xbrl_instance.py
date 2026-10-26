"""申报文件的完整 XBRL 实例 —— companyfacts 不收的**维度事实**。

companyfacts 只收不带维度的事实，于是两类数缺席：
  1. **分部收入**（按产品、经营分部、地区）：方法论预研做「营收 = 各分部之和」时没有历史可回算；
  2. **按类别申报的在外股数**（GOOGL、BRK 这类多类股公司没有不带维度的封面股数）。
两者都在每份 10-K / 10-Q 的 XBRL 实例里（内联 XBRL 申报的 `*_htm.xml`），是 SEC 原始申报，
有申报日，可以当账本主源。FMP 的分部接口没有申报日、免费档覆盖不全（2026-10-26 实测 CAT 402），
只配做核对。

只收单一维度（经营分部允许带 `ConsolidationItemsAxis = OperatingSegmentsMember` 限定）：
两个维度交叉的事实（某产品在某地区）不是分部合计。三个月的单季不收，与 companyfacts 同一口径。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ir_agent.ledger import Fact, Method
from ir_agent.sources.edgar_facts import _DURATION, CURRENCY, MONEY_UNIT, SHARE_UNIT
from ir_agent.sources.us_periods import fiscal_label

NS = {"x": "http://www.xbrl.org/2003/instance", "xbrldi": "http://xbrl.org/2006/xbrldi"}
_AXES = {"srt:ProductOrServiceAxis": "product",
         "us-gaap:StatementBusinessSegmentsAxis": "segment",
         "srt:StatementGeographicalAxis": "geo"}
_QUALIFIER = ("srt:ConsolidationItemsAxis", "us-gaap:OperatingSegmentsMember")
_CLASS_AXIS = "us-gaap:StatementClassOfStockAxis"
_REV_TAGS = _DURATION["revenue"]
_CONCEPTS = {**{t: (i, "seg_rev") for i, t in enumerate(_REV_TAGS)},
             "OperatingIncomeLoss": (0, "seg_op")}
_SHARES = "EntityCommonStockSharesOutstanding"
FORMS = ("10-K", "10-Q")
AXIS_CN = {"product": "产品", "segment": "经营分部", "geo": "地区"}
KIND_CN = {"seg_rev": "分部营收", "seg_op": "分部营业利润"}


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    filed: date
    report: date | None


def recent_filings(sub: dict, as_of: date) -> list[Filing]:
    """as_of 前最近一份 10-K 及其后的 10-Q（旧到新）。10-K 自带三年比较期，够历史回算。"""
    rec = (sub.get("filings") or {}).get("recent") or {}
    rows = []
    for i, form in enumerate(rec.get("form", [])):
        if form not in FORMS:
            continue
        filed = date.fromisoformat(rec["filingDate"][i])
        if filed > as_of:
            continue
        rd = rec.get("reportDate", [""] * (i + 1))[i]
        rows.append(Filing(rec["accessionNumber"][i], form, filed,
                           date.fromisoformat(rd) if rd else None))
    rows.sort(key=lambda f: f.filed)
    k = max((i for i, f in enumerate(rows) if f.form == "10-K"), default=None)
    return rows[k:] if k is not None else []


def instance_name(items: list[dict]) -> str | None:
    """申报目录里的 XBRL 实例文件：内联 XBRL 的 `*_htm.xml` 优先，否则唯一的非链接库 .xml。"""
    names = [it.get("name", "") for it in items]
    inline = [n for n in names if n.endswith("_htm.xml")]
    if inline:
        return inline[0]
    plain = [n for n in names if n.endswith(".xml") and n != "FilingSummary.xml"
             and not re.search(r"_(cal|def|lab|pre)\.xml$", n)]
    return plain[0] if len(plain) == 1 else None


def _local(qname: str) -> str:
    """'aapl:IPhoneMember' → 'IPhone'；'country:US' → 'US'。只留占位符允许的字符。"""
    name = qname.split(":", 1)[-1]
    name = name[:-6] if name.endswith("Member") else name
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _contexts(root) -> dict[str, tuple[date | None, date, tuple[tuple[str, str], ...]]]:
    out = {}
    for c in root.findall("x:context", NS):
        p = c.find("x:period", NS)
        s, e, i = (p.findtext(f"x:{t}", namespaces=NS) for t in ("startDate", "endDate", "instant"))
        dims = tuple((m.get("dimension"), (m.text or "").strip())
                     for m in c.findall(".//xbrldi:explicitMember", NS))
        out[c.get("id")] = (date.fromisoformat(s) if s else None, date.fromisoformat(e or i), dims)
    return out


def _segment_of(dims) -> tuple[str, str] | None:
    rest = [d for d in dims if d != _QUALIFIER]
    if len(rest) != 1 or rest[0][0] not in _AXES:
        return None
    return _AXES[rest[0][0]], _local(rest[0][1])


def parse_instance(xml_text: str, fye_mmdd: str, source_id: str, filed: date) -> list[Fact]:
    """分部事实（seg_rev / seg_op）与「按类别加总的在外股数」（仅当没有不带维度的封面股数时）。"""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    ctx = _contexts(root)
    best: dict[tuple[str, str], tuple[int, Decimal]] = {}
    plain_shares = False
    by_class: dict[date, list[tuple[str, Decimal]]] = {}
    for el in root:
        tag = el.tag.rsplit("}", 1)[-1]
        c = ctx.get(el.get("contextRef") or "")
        if c is None or el.text is None or not el.text.strip():
            continue
        start, end, dims = c
        if tag == _SHARES:
            if not dims:
                plain_shares = True
            elif len(dims) == 1 and dims[0][0] == _CLASS_AXIS:
                by_class.setdefault(end, []).append((_local(dims[0][1]), Decimal(el.text.strip())))
            continue
        if tag not in _CONCEPTS or start is None:
            continue
        seg = _segment_of(dims)
        label = fiscal_label(start, end, fye_mmdd) if seg else None
        if label is None:
            continue
        prio, kind = _CONCEPTS[tag]
        key = f"{kind}.{seg[0]}.{seg[1]}"
        if (key, label) not in best or prio < best[(key, label)][0]:
            best[(key, label)] = (prio, Decimal(el.text.strip()))
    out = [Fact(key=k, value=v, unit=MONEY_UNIT, currency=CURRENCY, period=p, as_of=filed,
                source_id=source_id, method=Method.REPORTED)
           for (k, p), (_prio, v) in sorted(best.items())]
    if not plain_shares and by_class:
        cover = max(by_class)
        parts = by_class[cover]
        out.append(Fact(key="shares_outstanding", value=sum((v for _, v in parts), Decimal(0)),
                        unit=SHARE_UNIT, period=cover.isoformat(), as_of=filed, source_id=source_id,
                        method=Method.COMPUTED,
                        derived_from=tuple(f"shares_outstanding[{n}]@{cover.isoformat()}" for n, _ in parts)))
    return out


def label(key: str) -> str | None:
    """seg_rev.product.IPhone → 分部营收（产品：IPhone）；不是分部键返回 None。"""
    parts = key.split(".")
    if len(parts) != 3 or parts[0] not in KIND_CN or parts[1] not in AXIS_CN:
        return None
    return f"{KIND_CN[parts[0]]}（{AXIS_CN[parts[1]]}：{parts[2]}）"


def load_dimensional(cik: str, sub: dict, fye_mmdd: str, store, as_of: date,
                     notes: list[str], latest_only: bool = False) -> list[Fact]:
    """最近一份 10-K 及其后 10-Q 的维度事实。单份失败记说明、不抛。
    latest_only：只读最新一份（同业补多类股股数只要封面那一份）。"""
    from ir_agent.sources import edgar

    cik_n = str(int(cik))
    out: list[Fact] = []
    filings = recent_filings(sub, as_of)
    for f in (filings[-1:] if latest_only else filings):
        base = f"https://www.sec.gov/Archives/edgar/data/{cik_n}/{f.accession.replace('-', '')}/"
        try:
            idx, _ = edgar.fetch_json_cached(base + "index.json", "sec_filing_index", store, as_of)
            name = instance_name(idx.get("directory", {}).get("item", []))
            if name is None:
                notes.append(f"{f.form} {f.accession}：目录里没有 XBRL 实例文件")
                continue
            text, sid = edgar.fetch_text_cached(base + name, "sec_xbrl_instance", store, as_of)
            out += parse_instance(text, fye_mmdd, sid, f.filed)
        except edgar.SecIdentityMissing:
            raise
        except Exception as e:                              # noqa: BLE001
            notes.append(f"{f.form} {f.accession}：XBRL 实例取不到或解析失败（{e.__class__.__name__}）")
    # 后一份申报把比较期原值再报一遍不是新版本（与 edgar_facts._facts 同一规则）
    last: dict[tuple[str, str], Decimal] = {}
    kept = []
    for x in sorted(out, key=lambda x: x.as_of):
        if last.get((x.key, x.period)) == x.value:
            continue
        last[(x.key, x.period)] = x.value
        kept.append(x)
    return kept
