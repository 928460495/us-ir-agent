"""可比同业的 10-K 叙述章节 → 语料库（**COMPANY 级**，用户 2026-10-26 决定）。

美股免费渠道没有卖方研报。竞争对手在 10-K 里对行业、竞争与需求的法定披露是最接近的替代：
比媒体可靠，但它是**对手自己的说法**、带立场，与本司自述同级 —— 能作事实依据（「行业产能
如何」），不能单独支撑假设。publisher 写明「同业 10-K」，读者与模型都分得清是谁在说。

只取 `r.comps_codes` 那几家（按市值就近选出的可比，六家）：全行业几十家的 10-K 正文放不进提示词，
也不需要。取 Item 1 业务、其中的竞争段落、Item 7 管理层讨论；时点之后申报的不取。
"""

from __future__ import annotations

from datetime import date

from ir_agent.sources.external import Document, Tier
from ir_agent.sources.tenk import extract_sections, latest_10k

PROMPT_LIMIT = 6000
_KEYS = ("business", "core_competency", "outlook", "industry_view")


def peer_10k_docs(r, as_of: date) -> tuple[list[Document], list[str]]:
    from ir_agent.sources import edgar

    docs: list[Document] = []
    notes: list[str] = []
    books = getattr(r, "peer_books", None) or {}
    for t in getattr(r, "comps_codes", None) or []:
        b = books.get(t)
        if b is None:
            continue
        try:
            sub = r.store.load(b.sids["submissions"]).payload
            tk = latest_10k(sub, b.cik, as_of)
            if tk is None:
                notes.append(f"{t} {b.name}：分析时点之前没有 10-K")
                continue
            html, _sid = edgar.fetch_text_cached(tk.url, "sec_10k_peer", r.store, as_of)
            ss = extract_sections(html, code=t)
        except edgar.SecIdentityMissing:
            raise
        except Exception as e:                              # noqa: BLE001
            notes.append(f"{t} {b.name}：同业 10-K 取不到（{e.__class__.__name__}）")
            continue
        parts = [f"【{s.label}】\n{s.text}" for k in _KEYS if (s := ss.get(k)) is not None]
        if not parts:
            notes.append(f"{t} {b.name}：10-K 里没抽到业务 / 竞争 / 管理层讨论章节")
            continue
        docs.append(Document(
            doc_id=f"peer10k_{t}_{tk.accession}", url=tk.url,
            title=f"{b.name} 10-K（财年截至 {tk.report_date.isoformat()}）：业务、竞争与管理层讨论",
            publisher=f"{b.name}（同业 10-K，竞争对手自述）", tier=Tier.COMPANY,
            published=tk.filed, retrieved=as_of, text="\n\n".join(parts), prompt_limit=PROMPT_LIMIT))
    return docs, notes
