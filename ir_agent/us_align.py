"""美股同业期间按**期末日期**对齐（设计「三.2」：同业对齐一律按报告期末日期判断）。

A 股全部 12 月财年，同名标签「2025FY」就是同一段时间；美股不是：苹果 FY2025 截止 2025-09-27，
戴尔 FY2025 截止 2025-01-31，同名差 8 个月。两种对齐各有用处：

- **年度**（行业聚合、画像、可比表）：同业取期末离本司财年末最近的那个财年，重贴到本司的
  FY 标签上 —— 之后 A 股那套按标签读年度数的代码原样可用（`calendarize`）。
- **TTM**（相对估值）：年初至今的累计长度各家不同，不能重贴标签；在同业自己的标签上算，
  截止季按日期挑（`align_quarter`），与 A 股「对齐本司截止季、没出就退一季」同一规则。
"""

from __future__ import annotations

from datetime import date, timedelta

from ir_agent.ledger import Fact

SAME_QUARTER_DAYS = 45      # 期末相差这么多天以内算同一季
LAG_DAYS = 135              # 最多落后一季（再早就不是「上一季」了）
FY_DAYS = 183               # 年度：期末相差半年以内才算同一年


def align_fy(ends: dict[str, date], target_end: date, visible: set[str]) -> str | None:
    """可见的 FY 标签里期末离 target_end 最近者；超过半年返回 None。"""
    cands = [(abs((ends[p] - target_end).days), p) for p in visible
             if p.endswith("FY") and p in ends]
    best = min(cands, default=None)
    return best[1] if best and best[0] <= FY_DAYS else None


def align_quarter(ends: dict[str, date], target_end: date,
                  visible: set[str]) -> tuple[str, bool] | None:
    """可见累计期间里期末不晚于本司截止季（+ 同季容差）的最新一期；落后超过一季返回 None。
    第二项为「落后一季」—— 页面逐家标出（A 股「用上一季 TTM」同一规则）。"""
    cands = [(ends[p], p) for p in visible if p in ends
             and target_end - timedelta(days=LAG_DAYS) <= ends[p] <= target_end + timedelta(days=SAME_QUARTER_DAYS)]
    if not cands:
        return None
    end, label = max(cands)
    return label, end < target_end - timedelta(days=SAME_QUARTER_DAYS)


def calendarize(book, target_ends: dict[str, date], as_of: date) -> dict[tuple[str, str], Fact]:
    """同业的 FY 流量与 FY 期末存量重贴到本司 FY 标签，返回 A 股 `peer_statements` 同形状的字典。

    对应关系是日历事实，与时点无关：映射时不按 as_of 过滤，否则同一个本司财年在不同时点会
    对到不同的同业财年。非 FY 期间不搬。

    字典每格只放一条事实，所以**版本按时点挑**：as_of 前可见的最新一版；一版都不可见时放最早
    那版（账本的时点守卫照样拦住它）。只留最后申报的一版会让重述把当时可见的原值挤掉。
    """
    fy_labels = {p for p in book.ends if p.endswith("FY")}
    mapping = {t: p for t, e in target_ends.items() if t.endswith("FY")
               if (p := align_fy(book.ends, e, fy_labels)) is not None}
    by_peer: dict[str, list[str]] = {}
    for t, p in mapping.items():
        by_peer.setdefault(p, []).append(t)
    out: dict[tuple[str, str], Fact] = {}
    for f in sorted(book.facts, key=lambda f: f.as_of):
        for t in by_peer.get(f.period, ()):
            have = out.get((f.key, t))
            if have is not None and f.as_of > as_of:
                continue                    # 已有一版；更晚、时点外的版本不顶替它
            out[(f.key, t)] = Fact(key=f.key, value=f.value, unit=f.unit, period=t, as_of=f.as_of,
                                   source_id=f.source_id, method=f.method, currency=f.currency,
                                   confidence=f.confidence, derived_from=f.derived_from,
                                   basis=f.basis)
    return out
