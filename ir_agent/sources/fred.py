"""美国宏观与利率：FRED / ALFRED。

**时点**：请求的版本窗口是 `[观测窗口起点, as_of]`（ALFRED），每个观测日期取 `realtime_end ==
as_of` 的那一版 —— 即 as_of 当天仍然有效的版本：GDP 初值，而不是三个月后的修订值。该版本的
`realtime_start` 就是它开始可见的日期，作为事实的 as_of。

注意 FRED 会把返回的 realtime_start / realtime_end **截到请求窗口内**：若窗口是
`[as_of, as_of]`，每条观测的生效日都会被截成 as_of，真实发布日就丢了（审查发现，此前即如此）。
A 股宏观只能按「统计期末 + 保守滞后」推算披露日，这里有真实的版本日期。

**指标**：ISM PMI、Conference Board 领先指数属付费数据，不用。领先 / 同步 / 滞后的归类按
CFA 三条定义（转折点先于 / 接近 / 晚于整体经济）套用到 FRED 可得指标，是本项目的映射。
A 股专有指标（PMI、M1/M2）美股路径不产出，周期打分按缺失维度跳过，阶段 2 再重配。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import requests

from ir_agent.ledger import Fact, Method
from ir_agent.sources.macro import (COINCIDENT, LAGGING, LEADING, MacroSnapshot,
                                     MacroUnavailable)

URL = "https://api.stlouisfed.org/fred/series/observations"
TIMEOUT = 20
FX = "汇率"
HISTORY = timedelta(days=366 * 6)            # 取六年：够算同比与近几年趋势；日度约 1560 个版本，低于 FRED 单次两千个的上限


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
    # 周期框架的利润与库存维度（A 股取不到）：企业税后利润（季度）、全行业库存销售比
    UsSeries("macro.corporate_profits_yoy", "CP", "yoy", "Q", COINCIDENT),
    UsSeries("macro.inventory_sales_ratio", "ISRATIO", "level", "M", LAGGING, "x"),
    UsSeries("macro.industrial_production_yoy", "INDPRO", "yoy", "M", COINCIDENT),
    UsSeries("macro.retail_sales_yoy", "RSAFS", "yoy", "M", COINCIDENT),
    UsSeries("macro.initial_claims", "ICSA", "level", "D", LEADING, "人"),
    UsSeries("macro.building_permits", "PERMIT", "level", "M", LEADING, "千套"),
    UsSeries("macro.consumer_sentiment", "UMCSENT", "level", "M", LEADING, "index"),
    # ISM PMI 付费、不用；地区联储的制造业调查免费且同为扩散指数（零为荣枯线），作先行指标替代。
    # OECD 美国综合领先指标在 FRED 上停更于 2024-01（2026-10-26 实测），不收。
    UsSeries("macro.philly_fed_mfg", "GACDFSA066MSFRBPHI", "level", "M", LEADING, "index"),
    UsSeries("macro.empire_state_mfg", "GACDISA066MSFRBNY", "level", "M", LEADING, "index"),
    # 美元汇率：境外收入占比高的公司（苹果过半）折算与定价都受它影响。ICE 的 DXY 是专有
    # 指数，FRED 上取美联储编制的广义美元指数（月均，可算同比）。不属于三分法，单列「汇率」组
    UsSeries("macro.usd_index", "TWEXBGSMTH", "level", "M", FX, "index"),
    UsSeries("macro.usd_index_yoy", "TWEXBGSMTH", "yoy", "M", FX),
    UsSeries("macro.term_spread", "T10Y2Y", "pct", "D", LEADING),
    UsSeries("macro.hy_spread", "BAMLH0A0HYM2", "pct", "D", LEADING),
    UsSeries("rate.ust_10y", "DGS10", "pct", "D", None),
    UsSeries("rate.ust_2y", "DGS2", "pct", "D", None),
)
US_INDICATOR_CLASS = {s.key: s.cls for s in US_MACRO if s.cls}

# FF49 行业 → 官方统计（人口普查局的制造业出货、美联储的分行业工业产出、零售销售）。
# 美股免费渠道没有卖方研报的行业测算；这些是政府口径的行业数，按当时版本入账，
# 键以 `macro.ind_` 开头 —— 与宏观同样挂在自己的月度期间上，进事实目录的规则不必另写。
# 只登记了阶段 4 试跑涉及的四个行业；其他行业返回空，如实说「无行业官方统计」。
_IND = {
    "computer_shipments": UsSeries("macro.ind_computer_shipments_yoy", "A34SVS", "yoy", "M", None),
    "computer_production": UsSeries("macro.ind_computer_production_yoy", "IPG334S", "yoy", "M", None),
    "semiconductor_production": UsSeries("macro.ind_semiconductor_production_yoy", "IPG3344S", "yoy", "M", None),
    "machinery_shipments": UsSeries("macro.ind_machinery_shipments_yoy", "A33SVS", "yoy", "M", None),
    "machinery_production": UsSeries("macro.ind_machinery_production_yoy", "IPG333S", "yoy", "M", None),
    "retail_sales": UsSeries("macro.ind_retail_sales_yoy", "RSXFS", "yoy", "M", None),
    "general_merchandise_sales": UsSeries("macro.ind_general_merchandise_sales_yoy", "RSGMS", "yoy", "M", None),
}
US_INDUSTRY: dict[int, tuple[UsSeries, ...]] = {
    35: (_IND["computer_shipments"], _IND["computer_production"]),
    37: (_IND["semiconductor_production"], _IND["computer_shipments"]),
    21: (_IND["machinery_shipments"], _IND["machinery_production"]),
    43: (_IND["retail_sales"], _IND["general_merchandise_sales"]),
}
IND_LABELS = {
    "macro.ind_computer_shipments_yoy": "计算机与电子产品制造商出货额同比（人口普查局）",
    "macro.ind_computer_production_yoy": "计算机与电子产品工业产出同比（美联储）",
    "macro.ind_semiconductor_production_yoy": "半导体及电子元件工业产出同比（美联储）",
    "macro.ind_machinery_shipments_yoy": "机械制造商出货额同比（人口普查局）",
    "macro.ind_machinery_production_yoy": "机械工业产出同比（美联储）",
    "macro.ind_retail_sales_yoy": "零售销售同比（人口普查局）",
    "macro.ind_general_merchandise_sales_yoy": "综合商品零售销售同比（人口普查局）",
}


def industry_series(ff49: int) -> tuple[UsSeries, ...]:
    return US_INDUSTRY.get(ff49, ())


def _all_specs() -> tuple[UsSeries, ...]:
    return US_MACRO + tuple(dict.fromkeys(s for v in US_INDUSTRY.values() for s in v))


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


def _realtime_end(as_of: date) -> str:
    """版本窗口的终点。分析时点是今天或昨天时取「当前有效版本」（9999-12-31）：FRED 按美国
    时间计日期，本机日期跑在前面时，realtime_end=今天 会被拒绝（HTTP 400，联网发现）。"""
    return "9999-12-31" if as_of >= date.today() - timedelta(days=1) else as_of.isoformat()


def _observations(series_id: str, as_of: date, store) -> tuple[list[dict], str]:
    start = (as_of - HISTORY).isoformat()
    # 版本窗口与观测窗口同起点：观测日期不早于该起点的数值不可能在起点之前公布，
    # 截断不改变任何返回观测的真实生效日；从 1776 年开窗则日度序列超出版本数上限。
    public = {"series_id": series_id, "file_type": "json",
              "realtime_start": start, "realtime_end": _realtime_end(as_of),
              "observation_start": start}
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
    specs = [s for s in _all_specs() if s.series_id == series_id]
    if not specs:
        raise ValueError(f"{series_id} 不在美国宏观与行业登记表里")
    obs, sid = _observations(series_id, as_of, store)
    points: dict[date, tuple[Decimal, date]] = {}
    current = _realtime_end(as_of)
    for o in obs:
        if o.get("realtime_end") != current:
            continue                         # 已被后续修订取代的旧版本
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


def history(series_id: str, start: date, end: date, store) -> list[tuple[date, Decimal]]:
    """不修订的行情类序列（如标普 500 SP500）的普通历史：不带版本参数，截到 end。

    这类序列取「当时版本」没有意义（指数不会被修订），日度又会撞上 FRED 单次两千个版本的上限。"""
    public = {"series_id": series_id, "file_type": "json",
              "observation_start": start.isoformat(), "observation_end": end.isoformat()}
    try:
        r = requests.get(URL, params={**public, "api_key": _key()}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise MacroUnavailable(f"FRED {series_id} 网络错误 {e.__class__.__name__}") from None
    if r.status_code != 200:
        raise MacroUnavailable(f"FRED {series_id} HTTP {r.status_code}")
    body = r.json()
    store.save(source=f"fred_{series_id.lower()}_history", payload=body, url=URL,
               fetched_at=datetime.now(), params=public)
    return [(date.fromisoformat(o["date"]), Decimal(o["value"])) for o in body.get("observations") or []
            if o.get("value") not in (None, "", ".")]


def fetch_industry_stats(ff49: int, as_of: date, store) -> tuple[list[Fact], list[str]]:
    """本司 FF49 行业的官方统计（当时版本）。单个序列失败只记说明。"""
    facts, notes = [], []
    for sid in dict.fromkeys(s.series_id for s in industry_series(ff49)):
        try:
            facts += [f for f in fetch_series_asof(sid, as_of, store) if f.key.startswith("macro.ind_")]
        except MacroUnavailable as e:
            notes.append(str(e))
    return facts, notes


CAGR_YEARS = 5
_QLABEL = re.compile(r"^(\d{4})Q([1-4])S$")


def nominal_gdp_cagr(facts: list[Fact]) -> list[Fact]:
    """名义 GDP 的五年复合增速（与 A 股 `macro._nominal_cagr` 同一用途：生命周期与永续增长率的
    参照系）。**同季对同季**：季调年化水平值也带残余季节性，跨季比不干净。每个季度各出一条，
    时点取两端较晚的公布日 —— 账本按 as_of 自然取到当时可见的最新一条。"""
    lv = {}
    for f in facts:
        m = _QLABEL.match(f.period) if f.key == "macro.gdp_nominal" else None
        if m:
            lv[(int(m.group(1)), int(m.group(2)))] = f
    out = []
    for (y, q), end in sorted(lv.items()):
        start = lv.get((y - CAGR_YEARS, q))
        if start is None or start.value <= 0:
            continue
        out.append(Fact(key="macro.gdp_nominal_cagr",
                        value=(end.value / start.value) ** (Decimal(1) / CAGR_YEARS) - 1,
                        unit="ratio", period=end.period, as_of=max(start.as_of, end.as_of),
                        source_id=end.source_id, method=Method.COMPUTED,
                        derived_from=(f"macro.gdp_nominal@{start.period}", f"macro.gdp_nominal@{end.period}")))
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
    return facts + nominal_gdp_cagr(facts), notes


class UsMacroSnapshot(MacroSnapshot):
    """美国宏观图景。指标名不写数字（「十年减两年」而非「10Y−2Y」）—— 提示词里的
    裸数字会被审计驳回，标签本身也不例外。"""

    _SHOWN = (("macro.term_spread", "期限利差（十年减两年）"),
              ("macro.hy_spread", "高收益债利差"),
              ("macro.initial_claims", "首次申领失业金人数"),
              ("macro.building_permits", "新屋开工许可"),
              ("macro.consumer_sentiment", "密歇根消费者信心"),
              ("macro.philly_fed_mfg", "费城联储制造业景气（扩散指数）"),
              ("macro.empire_state_mfg", "纽约联储制造业景气（扩散指数）"),
              ("macro.gdp_real_yoy", "实际 GDP 同比"),
              ("macro.gdp_nominal_yoy", "名义 GDP 同比"),
              ("macro.industrial_production_yoy", "工业产出同比"),
              ("macro.retail_sales_yoy", "零售销售同比"),
              ("macro.ppi_yoy", "PPI 同比"),
              ("macro.corporate_profits_yoy", "企业税后利润同比"),
              ("macro.inventory_sales_ratio", "库存销售比"),
              ("macro.unemployment", "失业率"),
              ("macro.cpi_yoy", "CPI 同比"),
              ("macro.usd_index", "广义美元指数"),
              ("macro.usd_index_yoy", "广义美元指数同比"))
    _classify = staticmethod(classify)
    _GROUPS = (LEADING, COINCIDENT, LAGGING, FX)

    @staticmethod
    def _fmt(f: Fact) -> str:
        from ir_agent.citation import format_value

        if f.unit == "ratio":
            return f"{f.value * 100:.2f}%"
        if f.unit == "index":
            return f"{f.value:.1f}"
        return format_value(f)
