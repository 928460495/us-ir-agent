"""8-K → 语料库（美股的「公司公告」）。

交出 `announce.AnnounceResult`，落盘与读回沿用 A 股公告的 `announce.save/load`。

按 8-K 的 item 分档（只排序，不影响引用与核验）:
  - 2.02 业绩发布：正文在附件 EX-99.1（新闻稿），主文档只有一句「见附件」；
  - 1.01 / 2.01 等实质事项：取主文档正文；
  - 其余（5.07 投票结果、9.01 单独的附件说明等）：程序性，排除并计数。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from ir_agent.sources import edgar
from ir_agent.sources.announce import AnnounceResult
from ir_agent.sources.external import Document, Tier
from ir_agent.sources.tenk import html_text

ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"
PROMPT_LIMIT = 8000

EARNINGS = "2.02"
ITEM_NAMES = {
    "2.02": "业绩发布", "1.01": "重大协议", "2.01": "资产收购或处置", "2.03": "直接财务义务",
    "2.05": "退出或处置成本", "2.06": "重大减值", "8.01": "其他事项",
}
TIER_ONE = {"2.02", "1.01", "2.01"}
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_HREF = re.compile(r'href="([^"]+)"', re.I)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)


def _exhibits(index_html: str) -> list[tuple[str, str]]:
    """申报索引页 → [(Type, 文档地址)]。主文档的链接带 /ix?doc= 前缀，去掉。"""
    out = []
    for row in _ROW.findall(index_html):
        cells = _CELL.findall(row)
        href = _HREF.search(row)
        if len(cells) < 4 or not href:
            continue
        kind = re.sub(r"<[^>]+>|&nbsp;", "", cells[3]).strip()
        path = href.group(1).replace("/ix?doc=", "")
        out.append((kind, "https://www.sec.gov" + path if path.startswith("/") else path))
    return out


def _earnings_url(index_html: str) -> str | None:
    ex = _exhibits(index_html)
    for kind, url in ex:
        if kind == "EX-99.1":
            return url
    return next((url for kind, url in ex if kind.startswith("EX-99")), None)


def gather_us(*, sub: dict, as_of: date, corpus, store, company: str,
              months: int = 12) -> AnnounceResult:
    res = AnnounceResult()
    r = sub.get("filings", {}).get("recent", {})
    cik = int(sub.get("cik") or 0)
    start = as_of - timedelta(days=round(months * 30.5))
    for i, form in enumerate(r.get("form", [])):
        if form not in ("8-K", "8-K/A"):
            continue
        filed = date.fromisoformat(r["filingDate"][i])
        if not start <= filed <= as_of:
            continue
        res.candidates += 1
        items = [x.strip() for x in (r.get("items", [""] * (i + 1))[i] or "").split(",") if x.strip()]
        picked = next((x for x in ITEM_NAMES if x in items), None)
        if picked is None:
            res.rejected += 1
            continue
        acc = r["accessionNumber"][i]
        base = ARCHIVE.format(cik=cik, acc=acc.replace("-", ""))
        try:
            if picked == EARNINGS:
                idx, _ = edgar.get_text(f"{base}/{acc}-index.htm", {}, "sec_8k_index", store)
                url = _earnings_url(idx)
                if url is None:
                    res.fetch_failed += 1
                    res.fetch_failed_reasons.append(f"8-K {acc}（{filed}）：索引页里没有 EX-99 附件")
                    continue
            else:
                url = f"{base}/{r['primaryDocument'][i]}"
            html, _ = edgar.get_text(url, {}, "sec_8k_doc", store)
        except edgar.SecIdentityMissing:
            raise
        except Exception as e:                               # noqa: BLE001
            res.fetch_failed += 1
            res.fetch_failed_reasons.append(f"8-K {acc}（{filed}）：{e.__class__.__name__}")
            continue
        doc = Document(doc_id=f"sec8k_{acc}", url=url, publisher=company, tier=Tier.COMPANY,
                       title=f"8-K {ITEM_NAMES[picked]}（{filed.isoformat()}）",
                       published=filed, retrieved=as_of, text=html_text(html),
                       prompt_limit=PROMPT_LIMIT, rank=1 if picked in TIER_ONE else 2)
        if corpus.add_if_visible(doc):
            res.docs.append(doc)
            res.picked += 1
    return res
