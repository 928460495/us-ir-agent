"""美股行业归属与同业集合：SIC → Fama-French 49 行业。

为什么用 FF49 而不是 SIC 本身: SIC 四位码太细（苹果 3571 电子计算机，同码上市公司只有
寥寥几家），两位码又太粗（35 同时装着工程机械与电脑）。FF49 把 SIC 区间归并成 49 个
业务实质相近的行业，是学术界做行业对照的通行口径（Fama & French 1997）。

同业枚举: 按目标所属 FF49 行业包含的每个 SIC 码，取 SEC「按 SIC 列公司」页面上的 CIK，
再与 SEC 代码表求交集 —— 只留有代码、在 NYSE / Nasdaq 上市的公司。不下载全量
submissions.zip。剔除项如实回报：同业集合少了谁会直接改变中位数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ir_agent.sources import edgar
from ir_agent.sources.industry import Classification, IndustryUnavailable, Member, Peers
from ir_agent.sources.taxonomy import MIN_PEERS

DATA = Path(__file__).resolve().parent.parent / "data" / "ff49_siccodes.txt"
BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
PAGE = 100
MAIN_EXCHANGES = ("NYSE", "Nasdaq")
_CIK_RE = re.compile(r"CIK=(\d{1,10})")

_ZH = {
    1: "农业", 2: "食品", 3: "糖果与饮料", 4: "酒类", 5: "烟草", 6: "休闲用品", 7: "娱乐",
    8: "印刷出版", 9: "消费品", 10: "服装", 11: "医疗服务", 12: "医疗器械", 13: "制药",
    14: "化工", 15: "橡胶塑料", 16: "纺织", 17: "建材", 18: "建筑", 19: "钢铁", 20: "金属制品",
    21: "机械", 22: "电气设备", 23: "汽车", 24: "航空器", 25: "船舶铁路设备", 26: "国防",
    27: "贵金属", 28: "非金属及工业金属采矿", 29: "煤炭", 30: "石油天然气", 31: "公用事业",
    32: "通信", 33: "个人服务", 34: "商业服务", 35: "计算机", 36: "软件", 37: "电子设备",
    38: "测量控制设备", 39: "办公用品", 40: "包装容器", 41: "运输", 42: "批发", 43: "零售",
    44: "餐饮酒店", 45: "银行", 46: "保险", 47: "房地产", 48: "金融交易", 49: "其他",
}


@lru_cache(maxsize=1)
def ff49_table() -> tuple[tuple[int, int, int, str, str], ...]:
    """(SIC 下限, 上限, 行业号, 英文简称, 英文全称)。"""
    out, cur = [], None
    for line in DATA.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        m = re.match(r"^\s*(\d{1,2})\s+(\S+)\s+(.+?)\s*$", line)
        r = re.match(r"^\s+(\d{4})-(\d{4})", line)
        if r:
            out.append((int(r.group(1)), int(r.group(2)), *cur))
        elif m:
            cur = (int(m.group(1)), m.group(2), m.group(3))
    return tuple(out)


def ff49_of(sic: int) -> tuple[int, str] | None:
    """SIC → (FF49 行业号, 「简称 中文名」)；不在任何区间返回 None。"""
    for lo, hi, num, short, _name in ff49_table():
        if lo <= sic <= hi:
            return num, f"{short} {_ZH[num]}"
    return None


def sic_codes_of(ff49: int) -> list[int]:
    return sorted({s for lo, hi, num, *_ in ff49_table() if num == ff49
                   for s in range(lo, hi + 1)})


def companies_with_sic(sic: int, store) -> set[str]:
    """SEC 按 SIC 列出的公司 CIK（10 位补零）。翻页直到某页不满或没有新公司。"""
    found: set[str] = set()
    start = 0
    while True:
        text, _ = edgar.get_text(BROWSE_URL, {"action": "getcompany", "SIC": sic, "type": "",
                                              "owner": "include", "count": PAGE, "start": start},
                                 "sec_browse_sic", store)
        page = {edgar.pad_cik(c) for c in _CIK_RE.findall(text)}
        new = page - found
        found |= page
        if len(page) < PAGE or not new:
            return found
        start += PAGE


@dataclass(frozen=True)
class UsClassification(Classification):
    def describe(self) -> str:
        return f"Fama-French 49 行业「{self.used_path}」（SIC {self.path}）"


def peer_set_us(ticker: str, store, min_peers: int = MIN_PEERS) -> Peers:
    """标的的 FF49 行业与同业集合，与 sources.industry.peer_set 同一返回类型。"""
    tickers, _ = edgar.load_tickers(store)
    me = tickers.get(edgar.normalize_ticker(ticker))
    if me is None:
        raise IndustryUnavailable(f"SEC 代码表里没有 {ticker}")
    sub, _ = edgar.fetch_submissions(me.cik, store)
    sic = str(sub.get("sic") or "")
    hit = ff49_of(int(sic)) if sic.isdigit() else None
    if hit is None:
        raise IndustryUnavailable(f"{ticker} 的 SIC {sic or '缺失'} 不在 Fama-French 49 行业定义内")
    num, label = hit
    used = f"FF49-{num} {label}"
    cls = UsClassification(code=edgar.normalize_ticker(ticker), name=me.name, path=sic,
                           used_path=used, level=1)
    out = Peers(target=edgar.normalize_ticker(ticker), target_name=me.name, classification=cls)

    by_cik: dict[str, list[str]] = {}
    for t, info in tickers.items():
        by_cik.setdefault(info.cik, []).append(t)
    seen: set[str] = set()
    for s in sic_codes_of(num):
        for cik in sorted(companies_with_sic(s, store) - seen):
            seen.add(cik)
            if cik == me.cik:
                continue
            codes = by_cik.get(cik)
            if not codes:
                out.excluded.append((cik, "", "SEC 代码表中无交易代码（多为已退市或未上市）"))
                continue
            info = tickers[codes[0]]                  # 多类股（BRK-A / BRK-B）只计一家
            if info.exchange not in MAIN_EXCHANGES:
                out.excluded.append((codes[0], info.name, f"交易所 {info.exchange or '未知'}，非 NYSE / Nasdaq"))
                continue
            out.members.append(Member(codes[0], info.name))
    if not out.members:
        raise IndustryUnavailable(f"{ticker}: 行业「{used}」下没有可用同业")
    out.thin = len(out.members) < min_peers
    return out
