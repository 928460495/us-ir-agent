"""10-K 叙述章节 —— 美股战略与产业链分析的证据基础。

与 A 股 `annual_sections` 交出**同一个** `SectionSet`（键名沿用），下游 strategy /
value_chain / 引文核验不改接口。对应关系:

    business         ← Item 1   Business
    risks            ← Item 1A  Risk Factors
    outlook          ← Item 7   MD&A
    market_risk      ← Item 7A  Quantitative and Qualitative Disclosures About Market Risk
    core_competency  ← Item 1 里以「Competition」起头的小节
    industry_view    ← 不抽（10-K 没有对应章节），如实记缺

**定位**: 只认行首的「Item N」作为章节边界；同一个 Item 多处命中时（目录 + 正文），
取到下一个 Item 标题之间正文最长的那一处 —— 目录项的「正文」只有一个页码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser

from ir_agent.sources.annual_sections import Section, SectionSet, SECTION_LABELS

ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
# 英文信息密度约为中文一半，摘录上限取中文年报（3000 字）的两倍
MAX_EXCERPT = 6000

_ITEMS = {"business": "1", "risks": "1A", "outlook": "7", "market_risk": "7A"}
_ANY_ITEM = re.compile(r"(?im)^\s*item\s+(\d{1,2}[a-c]?)\b\.?")
# 页脚，如「Apple Inc. | 2025 Form 10-K | 20」
_FOOTER = re.compile(r"(?im)^.{0,80}\|\s*\d{4}\s+form\s+10-k\s*\|\s*\d+\s*$\n?")
_INCORPORATED = re.compile(r"(?i)incorporated\s+(?:herein\s+)?by\s+reference")
INCORPORATED_MAX = 400
_BLOCK = {"p", "div", "tr", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
          "table", "section"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "ix:header"):
            self.skip += 1
        if tag in _BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "ix:header"):
            self.skip = max(0, self.skip - 1)
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_text(html: str) -> str:
    """去标签；块级元素换行；内嵌 XBRL 头（ix:header）与脚本不进正文。"""
    p = _Text()
    p.feed(html)
    t = "".join(p.out).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    return re.sub(r"\n{2,}", "\n", t).strip()


@dataclass(frozen=True)
class TenK:
    url: str
    filed: date
    report_date: date
    accession: str


def latest_10k(sub: dict, cik: str, as_of: date) -> TenK | None:
    """as_of 时点可见的最新一期年报的**原版 10-K**；该期只有 10-K/A 时才用它。

    叙述章节不取修订版：10-K/A 多数只补 Part III（高管薪酬、治理）或附件，主文档里没有
    Item 1 / 1A / 7（审查发现）。数字的修订已由 XBRL 的版本机制处理，与这里无关。"""
    r = sub.get("filings", {}).get("recent", {})
    rows = []
    for i, form in enumerate(r.get("form", [])):
        if form not in ("10-K", "10-K/A"):
            continue
        filed = date.fromisoformat(r["filingDate"][i])
        if filed > as_of:
            continue
        rows.append((date.fromisoformat(r["reportDate"][i]), form == "10-K", filed, i))
    if not rows:
        return None
    _rep, _orig, filed, i = max(rows)            # 最新一期 → 原版优先 → 最晚申报
    acc = r["accessionNumber"][i]
    return TenK(url=ARCHIVES.format(cik=int(cik), acc=acc.replace("-", ""),
                                    doc=r["primaryDocument"][i]),
                filed=filed, report_date=date.fromisoformat(r["reportDate"][i]),
                accession=acc)


def _bodies(text: str, item: str) -> list[str]:
    """某个 Item 每一处命中的正文（到下一个 Item 标题为止，不含标题行）。"""
    heads = [(m.start(), m.end(), m.group(1).upper()) for m in _ANY_ITEM.finditer(text)]
    out = []
    for k, (s, e, n) in enumerate(heads):
        if n != item:
            continue
        stop = heads[k + 1][0] if k + 1 < len(heads) else len(text)
        line_end = text.find("\n", e)
        body = text[line_end + 1 if line_end != -1 else e:stop].strip()
        out.append(body)
    return out


def _competition(business: str) -> str | None:
    m = re.search(r"(?m)^Competition\s*$", business)
    if not m:
        return None
    rest = business[m.end():].lstrip("\n")
    lines, out = rest.split("\n"), []
    for line in lines:
        # 下一个小节标题：短、不以句号结尾
        if out and len(line) <= 60 and not line.rstrip().endswith((".", ":", ";")):
            break
        out.append(line)
    return "\n".join(out).strip() or None


def extract_sections(html: str, code: str = "", max_chars: int = MAX_EXCERPT) -> SectionSet:
    text = _FOOTER.sub("", html_text(html))
    out = SectionSet(code=code, pages=0)
    full: dict[str, str] = {}
    for key, item in _ITEMS.items():
        bodies = _bodies(text, item)
        body = max(bodies, key=len, default="")
        if body and len(body) < INCORPORATED_MAX and _INCORPORATED.search(body):
            out.missing.append(key)
            out.notes[key] = "以引用方式并入（正文在附件中，本文件只有一句指针）"
            continue
        if len(body) < 80:
            out.missing.append(key)
            continue
        full[key] = body
    comp = _competition(full.get("business", ""))
    if comp:
        full["core_competency"] = comp
    else:
        out.missing.append("core_competency")
    out.missing.append("industry_view")
    out.notes["industry_view"] = "10-K 不设此节（行业格局散见于 Item 1 业务与 MD&A），相关判断需另寻依据或明说无据"
    for key in ("business", "risks", "outlook", "market_risk", "core_competency"):
        if key not in full:
            continue
        body = full[key]
        out.sections[key] = Section(key=key, label=SECTION_LABELS[key], heading=key, page=0,
                                    text=body[:max_chars], truncated=len(body) > max_chars)
    return out
