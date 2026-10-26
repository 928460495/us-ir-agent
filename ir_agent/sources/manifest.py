# -*- coding: utf-8 -*-
"""门① 的来源清单 —— 人要能逐条点开核。

**为什么要有这道门。** 实测（生益科技 2026-10-06）: 人只在最后一道门介入，
而那时资料与结论都已固定。而「em_macro / eastmoney_report 缺原始接口快照」
这条告警连续六轮只在终端一闪而过 —— 它是**来源质量的事实**，归这道门；
不是本轮的异常，不归终端。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ir_agent.sources.external import Tier

# 结构性缺口：这些来源的数字从文本抽出，未保留原始接口响应。
# **每轮必响的告警等于没有告警**（与 research._mark 那次教训同一条）——
# 它属于「来源质量」，在门① 说一次，不在每轮终端重复。
KNOWN_GAPS = {
    "em_macro": "宏观指标从页面文本抽出，未保留原始接口响应",
    "eastmoney_report": "研报数字从 PDF 段落抽出，未保留原始接口响应",
}


@dataclass
class SourceRow:
    kind: str          # 账本事实 | 外部文档 | 公司公告 | 年报 PDF | 同业样本
    label: str
    origin: str        # 接口名 或 URL
    local: str         # 本地文件路径；空串 = 无本地缓存
    retrieved: str     # 抓取/披露日期
    note: str          # 分级、截断、降级说明等


def missing_files(rows: list[SourceRow]) -> list[str]:
    """清单里列出的本地文件，逐个确认真的在磁盘上。

    **防的是「清单引用了一个从未保存的文件」。** 人点开发现打不开，这道门
    就白设了。没有本地缓存的行（只有 URL 的外部网页）不算缺失。
    """
    out: list[str] = []
    for r in rows:
        if r.local and not Path(r.local).exists():
            out.append(f"[{r.kind}] {r.label}：清单列出的本地文件不存在 "
                       f"—— {r.local}")
    return out


# 分节顺序。COMPANY 级文档按渠道再分：本司公告、本司电话会纪要、竞争对手的法定披露 ——
# 同级不同源，混在「公司公告」里会让人把对手的 10-K 当成本司公告去核。
KINDS = ("账本事实", "外部文档", "公司公告", "电话会纪要（二手转存）", "同业披露（竞争对手 10-K）",
         "年报 PDF", "年报（10-K）", "同业样本")
_CHANNEL_KIND = {"transcript": "电话会纪要（二手转存）", "peer_filing": "同业披露（竞争对手 10-K）"}


def render(rows: list[SourceRow], gaps: list[tuple[str, int]]) -> str:
    """生成 sources.md。`gaps` 是 (来源名, 本轮涉及的引用条数)。"""
    lines = ["# 资料来源清单（门①：请逐条核验）", "",
             "本清单给出每份资料的出处与本地文件位置。**核验通过后，"
             "用 `--verified-by <你的名字>` 进入下一阶段。**", ""]
    if gaps:
        lines += ["## ⚠ 以下来源不具备完整可溯源性", "",
                  "这是**结构性限制，非本轮故障** —— 数字可溯源到账本条目，"
                  "但反查不到当时的接口响应：", ""]
        for name, n in gaps:
            why = KNOWN_GAPS.get(name, "未保留原始接口响应")
            lines.append(f"- `{name}`：{why}（本轮涉及 {n} 条引用）")
        lines.append("")
    for kind in KINDS:
        part = [r for r in rows if r.kind == kind]
        if not part:
            continue
        lines += [f"## {kind}（{len(part)} 项）", "",
                  "| 名称 | 出处 | 本地文件 | 日期 | 备注 |",
                  "| --- | --- | --- | --- | --- |"]
        for r in part:
            local = r.local or "—"
            lines.append(f"| {r.label} | {r.origin} | {local} | "
                         f"{r.retrieved} | {r.note} |")
        lines.append("")
    return "\n".join(lines)


def collect_rows(ledger, corpus=None, pdf_dir=None, peers: tuple = (),
                 annual: tuple | None = None, store=None, code: str = "") -> list[SourceRow]:
    """从账本、语料库、公告缓存、年报、同业名单汇总清单行。

    取不到的部分留空而不是编 —— 清单的价值在于它说的每一条都能点开。

    `pdf_dir` 是**公告**的 PDF 缓存（`announce.gather` 写入），用来给每篇
    公告找本地文件；**它不是年报目录**。`annual` 是 `(年报网址, 本地缓存
    路径)`：年报由 `_load_sections` 缓存到别处。`annual=None` 表示调用方
    不提供年报信息（不出这一节）；`("", None)` 表示找过、没有年报公告 ——
    会列一行说出来。
    """
    rows: list[SourceRow] = []
    # **`_facts` 是 `(key, period) -> list[Fact]`，值是一个桶而不是单条事实**
    # （`ledger.py:63`：`dict[tuple[str, str], list[Fact]]`，`put` 往
    # `setdefault(..., [])` 里 append）。写成 `f = ledger._facts[(key, per)]`
    # 再 `getattr(f, "source_id", "")`，拿的是 list 的属性 —— 恒得空串。
    # 后果有两层，都不报错：每条账本事实的「出处」是空的（而这张清单的全部
    # 价值就是每一条都能点开），且下面按 sid 去重时所有行都撞在 "" 上，
    # 整个账本事实小节只会剩一行。
    # **一个快照一行**，标它支撑了几条事实、举一条为例，「本地文件」指向快照本身。
    # 此前每个快照只列它碰巧排第一的那条事实（AAPL 实跑：整份 companyfacts 显示成
    # 「revenue@2007FY」），本地文件一律空着 —— 人既看不出这一行代表什么，也点不开。
    by_sid: dict[str, list] = {}
    for (key, per), bucket in getattr(ledger, "_facts", {}).items():  # noqa: SLF001
        for f in bucket:
            by_sid.setdefault(getattr(f, "source_id", "") or "", []).append((key, per, f))
    for sid, items in by_sid.items():
        key, per, f = max(items, key=lambda t: str(getattr(t[2], "as_of", "")))
        local = ""
        if store is not None and sid and (p := store.path_for(sid)).exists():
            local = str(p)
        rows.append(SourceRow(
            kind="账本事实", label=f"{len(items)} 条事实（如 {key}@{per}）", origin=sid,
            local=local, retrieved=str(getattr(f, "as_of", "")), note=""))
    for d in (getattr(corpus, "docs", None) or []):
        # **按枚举成员比，不要按 str() 比。** `Tier` 是 `(str, Enum)` 混入类，
        # `str(Tier.COMPANY)` 给的是 `'Tier.COMPANY'` 而不是 `'COMPANY'`
        # （值是小写的 `'company'`）—— 写成 `str(d.tier) == "COMPANY"` 恒为
        # False，于是门① 清单里「公司公告」那一节永远是空的、所有公告都被
        # 归进「外部文档」。无异常、无告警，只有读清单的人看到一个空小节。
        is_ann = getattr(d, "tier", None) is Tier.COMPANY
        url = getattr(d, "url", "") or ""
        # **`Document` 没有本地路径字段。** 上一版写 `getattr(d, "path", "")`
        # —— 恒得空串，生益科技实跑时 20 篇公告的「本地文件」全是 `—`，
        # 而 PDF 明明缓存着。`announce.gather` 用 `_download(url, cache_dir)`
        # 缓存，文件名就是网址末段。没缓存的留空，不编路径。
        local = ""
        if is_ann and pdf_dir and (name := url.rsplit("/", 1)[-1]):
            if (p := Path(pdf_dir) / name).exists():
                local = str(p)
        rows.append(SourceRow(
            kind=(_CHANNEL_KIND.get(getattr(d, "channel", ""), "公司公告") if is_ann else "外部文档"),
            label=(getattr(d, "title", "") or "")[:40],
            origin=url,
            local=local,
            retrieved=str(getattr(d, "published", "") or ""),
            note=str(getattr(d, "publisher", "") or "")))
    # **年报由年报地址定位，不扫缓存目录。** 上一版扫 `pdf_dir` 的 `*.pdf`
    # 当年报：那个目录里是公告；而文件名是大写 `.PDF`、glob 区分大小写，
    # 生益科技实跑时一个都没匹配上，这一节整个消失。
    from ir_agent.market import is_us
    us = bool(code) and is_us(code)
    ann_kind = "年报（10-K）" if us else "年报 PDF"
    if annual is not None:
        url, path = annual
        if not url:
            rows.append(SourceRow(
                kind=ann_kind, label="未找到年报公告", origin="—", local="",
                retrieved="", note="战略与产业链分析缺年报叙述章节"))
        else:
            cached = path is not None and Path(path).exists()
            rows.append(SourceRow(
                kind=ann_kind, label=url.rsplit("/", 1)[-1] or "年报",
                origin=url, local=str(path) if cached else "", retrieved="",
                # 未缓存时**不写路径**：写一个不存在的路径，`missing_files`
                # 会把它报成「清单引用了从未保存的文件」—— 那不对。
                note="" if cached else "未在本地缓存"))
    if peers:
        rows.append(SourceRow(
            kind="同业样本", label=f"{len(peers)} 家",
            origin="SEC SIC → Fama-French 49 行业（NYSE / Nasdaq 上市）" if us else "东财行业分类",
            local="", retrieved="",
            note=("样本偏小，行业中位数代表性弱" if len(peers) < 15 else "")))
    return rows
