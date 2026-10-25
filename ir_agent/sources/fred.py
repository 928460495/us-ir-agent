"""美国宏观与利率：FRED / ALFRED。

**时点**：请求带 `realtime_start = realtime_end = as_of`（ALFRED 的「当时版本」），返回的
是截至 as_of 当天官方公布过的数值 —— GDP 初值、而不是三个月后的修订值。每个观测的
`realtime_start` 就是该数值开始可见的日期，直接作为事实的 as_of。A 股宏观只能按「统计期末
+ 保守滞后」推算披露日，这里有真实的版本日期。

**指标**：ISM PMI、Conference Board 领先指数属付费数据，不用。领先 / 同步 / 滞后的归类按
CFA 三条定义（转折点先于 / 接近 / 晚于整体经济）套用到 FRED 可得指标，是本项目的映射。
A 股专有指标（PMI、M1/M2）美股路径不产出，周期打分按缺失维度跳过，阶段 2 再重配。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import requests

from ir_agent.ledger import Fact, Method
from ir_agent.sources.macro import COINCIDENT, LAGGING, LEADING, MacroUnavailable

URL = "https://api.stlouisfed.org/fred/series/observations"
TIMEOUT = 20
HISTORY = timedelta(days=366 * 6)            # 取六年：够算同比与近几年趋势


@dataclass(frozen=True)
class UsSeries:
    key: str
    series_id: str
    transform: str          # "yoy" 同比 | "level" 原值 | "pct" 百分数转小数
    freq: str               # "M" 月 | "Q" 季（单季）| "D" 日 / 周
    cls: str | None         # 领先 / 同步 / 滞后；利率不归类
    unit: str = "ratio"
    scale: Decimal = Decimal(1)


US_MACRO: tuple[UsSeries, ...] = (
    UsSeries("macro.cpi_yoy", "CPIAUCSL", "yoy", "M", LAGGING),
    UsSeries("macro.ppi_yoy", "PPIACO", "yoy", "M", COINCIDENT),
    UsSeries("macro.gdp_real_yoy", "GDPC1", "yoy", "Q", COINCIDENT),
    UsSeries("macro.gdp_nominal_yoy", "GDP", "yoy", "Q", COINCIDENT),
    # FRED 的 GDP 以十亿美元计（季调年化）
    UsSeries("macro.gdp_nominal", "GDP", "level", "Q", COINCIDENT, "美元", Decimal(10) ** 9),
    UsSeries("macro.unemployment", "UNRATE", "pct", "M", LAGGING),
    UsSeries("macro.industrial_production_yoy", "INDPRO", "yoy", "M", COINCIDENT),
    UsSeries("macro.retail_sales_yoy", "RSAFS", "yoy", "M", COINCIDENT),
    UsSeries("macro.initial_claims", "ICSA", "level", "D", LEADING, "人"),
    UsSeries("macro.building_permits", "PERMIT", "level", "M", LEADING, "千套"),
    UsSeries("macro.consumer_sentiment", "UMCSENT", "level", "M", LEADING, "index"),
    UsSeries("macro.term_spread", "T10Y2Y", "pct", "D", LEADING),
    UsSeries("macro.hy_spread", "BAMLH0A0HYM2", "pct", "D", LEADING),
    UsSeries("rate.ust_10y", "DGS10", "pct", "D", None),
    UsSeries("rate.ust_2y", "DGS2", "pct", "D", None),
)
US_INDICATOR_CLASS = {s.key: s.cls for s in US_MACRO if s.cls}


def classify(key: str) -> str | None:
    return US_INDICATOR_CLASS.get(key)


def _key() -> str:
    k = os.environ.get("FRED_API_KEY", "").strip()
    if not k:
        raise MacroUnavailable("FRED 不可用：未设置 FRED_API_KEY")
    return k


def _period(d: date, freq: str) -> str:
    if freq == "M":
        return f"{d.year}-{d.month:02d}"
    if freq == "Q":
        return f"{d.year}Q{(d.month - 1) // 3 + 1}S"        # 单季，与累计口径的 Q1/H1 区分
    return d.isoformat()


def _observations(series_id: str, as_of: date, store) -> tuple[list[dict], str]:
    public = {"series_id": series_id, "file_type": "json",
              "realtime_start": as_of.isoformat(), "realtime_end": as_of.isoformat(),
              "observation_start": (as_of - HISTORY).isoformat()}
    try:
        r = requests.get(URL, params={**public, "api_key": _key()}, timeout=TIMEOUT)
    except requests.RequestException as e:       # 异常文本可能带 key，不透传
        raise MacroUnavailable(f"FRED {series_id} 网络错误 {e.__class__.__name__}") from None
    if r.status_code != 200:
        raise MacroUnavailable(f"FRED {series_id} HTTP {r.status_code}")
    body = r.json()
    sid = store.save(source=f"fred_{series_id.lower()}", payload=body, url=URL,
                     fetched_at=datetime.now(), params=public)
    return body.get("observations") or [], sid


def fetch_series_asof(series_id: str, as_of: date, store) -> list[Fact]:
    """一个 FRED 序列在 as_of 当时的版本 → 登记表里用到它的所有账本键。"""
    specs = [s for s in US_MACRO if s.series_id == series_id]
    if not specs:
        raise ValueError(f"{series_id} 不在美国宏观登记表里")
    obs, sid = _observations(series_id, as_of, store)
    points: dict[date, tuple[Decimal, date]] = {}
    for o in obs:
        if o.get("value") in (None, "", "."):
            continue
        points[date.fromisoformat(o["date"])] = (Decimal(o["value"]),
                                                 date.fromisoformat(o["realtime_start"]))

    out = []
    for s in specs:
        for d, (v, seen) in sorted(points.items()):
            if s.transform == "yoy":
                prev = points.get(date(d.year - 1, d.month, d.day))
                if prev is None or prev[0] == 0:
                    continue
                value, avail = v / prev[0] - 1, max(seen, prev[1])
            elif s.transform == "pct":
                value, avail = v / 100, seen
            else:
                value, avail = v * s.scale, seen
            out.append(Fact(key=s.key, value=value, unit=s.unit, period=_period(d, s.freq),
                            as_of=avail, source_id=sid, method=Method.REPORTED,
                            currency="USD" if s.unit == "美元" else None))
    return out


def fetch_us_macro(as_of: date, store) -> tuple[list[Fact], list[str]]:
    """登记表全部序列。单个序列失败只记说明 —— 宏观是背景，不是主线。缺 key 整体不可用。"""
    _key()
    facts, notes = [], []
    for sid in dict.fromkeys(s.series_id for s in US_MACRO):
        try:
            facts += fetch_series_asof(sid, as_of, store)
        except MacroUnavailable as e:
            notes.append(str(e))
    return facts, notes
