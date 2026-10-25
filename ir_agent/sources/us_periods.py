"""美股财年期间标签：由起止日期 + 公司财年末推出。

为什么不用 companyfacts 自带的 `fy` / `fp`：那是**申报文件**的财年与期间。2025 年 10-K
里附带的 2024 年比较数据，`fy` 也是 2025 —— 直接用会把比较期标成当年，静默错位。

美股的两个特殊情况:
  1. 非自然年财年。苹果截止 9 月最后一个周六左右，英伟达截止 1 月下旬（FY2026 截止于
     2026-01-25，财年名取截止日所在年份）。
  2. 52/53 周财年。截止日每年漂移几天；若漂到 1 月 1–7 日（「最接近 12 月 31 日的周六」），
     该财年实质是上一个自然年，归上一年。

标签与 A 股同一套累计口径：Q1 / H1 / Q1-Q3 / FY。单独披露的三个月数据（如第二季单季）
不直接入账，单季由 H1 − Q1 拆出。
"""

from __future__ import annotations

import calendar
from datetime import date

TOLERANCE_DAYS = 16                         # 52/53 周财年截止日的漂移
# 自财年起点累计的天数 → 第几季（含）。区间放宽到能容纳零售业的 12/12/12/16 周季度
# （Costco 前三季累计 252 天，按「九个月 ± 16 天」会被丢掉 —— 联网验收发现）。
# 区间互不重叠；单季三个月的数据靠「起点须贴着财年起点」排除，不靠长度。
_RANGES = ((1, 70, 110), (2, 155, 200), (3, 235, 290), (4, 350, 380))
_LABELS = {1: "Q1", 2: "H1", 3: "Q1-Q3", 4: "FY"}


def _parse_fye(fye_mmdd: str) -> tuple[int, int]:
    if len(fye_mmdd) != 4 or not fye_mmdd.isdigit():
        raise ValueError(f"财年末应为 MMDD 四位数字，得到 {fye_mmdd!r}")
    m, d = int(fye_mmdd[:2]), int(fye_mmdd[2:])
    if not 1 <= m <= 12 or not 1 <= d <= 31:
        raise ValueError(f"财年末不是有效日期: {fye_mmdd!r}")
    return m, d


def _nominal_fye(year: int, m: int, d: int) -> date:
    """财年 `year` 的名义截止日。截止于 1 月 1–7 日的财年视为截止于上一年 12 月 31 日。"""
    if m == 1 and d <= 7:
        return date(year, 12, 31)
    return date(year, m, min(d, calendar.monthrange(year, m)[1]))


def fiscal_year(end: date, fye_mmdd: str) -> int:
    """`end` 所属的财年：名义截止日（含漂移容差）不早于 `end` 的最早那一年。"""
    m, d = _parse_fye(fye_mmdd)
    for y in (end.year - 1, end.year, end.year + 1):
        if (end - _nominal_fye(y, m, d)).days <= TOLERANCE_DAYS:
            return y
    raise AssertionError("unreachable")       # end.year + 1 的名义截止日总在 end 之后


def _quarters(days: int) -> int | None:
    """自财年起点累计的天数 → 1–4 个季度；不在任何区间返回 None。"""
    return next((q for q, lo, hi in _RANGES if lo <= days <= hi), None)


def fiscal_label(start: date | None, end: date, fye_mmdd: str) -> str | None:
    """期间标签，如 `2025Q1` / `2025FY`；不是标准累计期间则返回 None。

    `start is None` 表示时点数（资产负债表、股本）。期间数要求从财年起点开始累计。
    """
    m, d = _parse_fye(fye_mmdd)
    fy = fiscal_year(end, fye_mmdd)
    fy_start = _nominal_fye(fy - 1, m, d)      # 上一财年名义截止日，即本财年起点的前一天
    if start is not None and abs((start - fy_start).days - 1) > TOLERANCE_DAYS:
        return None                             # 不是从财年起点开始累计的
    q = _quarters((end - fy_start).days)
    return f"{fy}{_LABELS[q]}" if q else None
