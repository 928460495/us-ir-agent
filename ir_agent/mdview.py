"""人读文件的排版：任何 Markdown → 规整 Markdown（`normalize`）→ HTML（`to_html`）。

spec 2026-11-25-readable-layout。模型的输出是「大体按格式、夹带自由发挥」：小标题至少三种写法，
一行一条却不空行（渲染后粘成一段），还有把每行包在反引号或代码围栏里的。这一层**不认识任何字段、
公司或行业**，只管分块与空行；认不出的就是普通段落，原样显示。

**一字不丢优先于好看。** 语法残缺（没闭合的 `**`、列数不齐的表格、没收尾的围栏）一律按普通文本处理。
"""

from __future__ import annotations

import html as _html
import re

LEVELS = ("通过", "提醒", "致命", "待采集")
# 审核意见的一行（`Verdict.line()` 的写法）：[类别] 条目：级别　理由（⚠ …）　建议：…
REVIEW_RX = re.compile(rf"^\s*(?:[-*•·]\s*)?\[(?P<kind>[^\]\[|]{{1,8}})\]\s*(?P<subj>.+?)[：:]\s*"
                       rf"(?P<level>{'|'.join(LEVELS)})(?:[\s　]+(?P<rest>.*))?$", re.S)
_SUGGEST = re.compile(r"[\s　]*建议\s*[:：]\s*")
_QUOTE_NOTE = re.compile(r"（(⚠[^（）]*(?:（[^（）]*）[^（）]*)*)）\s*$", re.S)

HEADING_RX = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_BOLD_LINE = re.compile(r"^\*\*([^*]{1,30})\*\*[:：]?$")
_BRACKET_LINE = re.compile(r"^【([^】]{1,30})】$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_FENCE = re.compile(r"^\s*```(.*)$")
_ITEM = re.compile(r"^(\s*)(?:[-*•·]|(\d{1,2})[.、)）])\s+(.*)$")
_DOT_ITEM = re.compile(r"^(\s*)[·•–]\s*(\S.*)$")
_TABLE = re.compile(r"^\s*\|.*\|\s*$")
_FIELD_LIKE = re.compile(r"^[`*\s]*[一-鿿]{2,6}[*\s]*[:：]")
_WRAPPED = re.compile(r"^\s*`([^`]+)`\s*$")


def _review(m: re.Match[str]) -> list[str]:
    rest = (m.group("rest") or "").strip()
    out = [f"- **[{m.group('kind')}] {m.group('subj').strip()}**：{m.group('level')}"]
    why, *sug = _SUGGEST.split(rest, maxsplit=1)
    note = ""
    if (q := _QUOTE_NOTE.search(why.strip())):
        note, why = q.group(1), why.strip()[:q.start()]
    for lab, val in (("理由", why), ("引文核对", note), ("建议", sug[0] if sug else "")):
        if val.strip(" 　"):
            out.append(f"  - {lab}：{' '.join(val.split())}")
    return out


def _is_sentence(s: str) -> bool:
    return s.rstrip().endswith(("。", "！", "？", "；", "."))


def _unwrap(lines: list[str]) -> list[str]:
    """去掉包住字段行的围栏与整行反引号；没收尾的围栏当普通文本；其他围栏原样。"""
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if m:
            j = next((k for k in range(i + 1, len(lines)) if _FENCE.match(lines[k])), None)
            if j is None:                                  # 没收尾：丢掉围栏行本身，内容照排
                if m.group(1).strip():
                    out.append(lines[i])
                i += 1
                continue
            inner = lines[i + 1:j]
            body = [x for x in inner if x.strip()]
            if not m.group(1).strip() and body and all(_FIELD_LIKE.match(x) for x in body):
                out += inner
            else:
                out += ["\x00" + x for x in lines[i:j + 1]]         # 标记：代码块，原样
            i = j + 1
            continue
        w = _WRAPPED.match(lines[i])
        out.append(w.group(1) if w and _FIELD_LIKE.match(w.group(1)) else lines[i])
        i += 1
    return out


def blocks(text: str) -> list[tuple[str, list[str]]]:
    """切成 [(种类, 行)]，种类 ∈ head|list|quote|table|code|para。"""
    out: list[tuple[str, list[str]]] = []

    def put(kind: str, lines: list[str], join: bool) -> None:
        if join and out and out[-1][0] == kind and not fresh[0]:
            out[-1][1].extend(lines)
        else:
            out.append((kind, list(lines)))
        fresh[0] = False

    fresh = [True]
    for ln in _unwrap((text or "").replace("\r\n", "\n").split("\n")):
        if ln.startswith("\x00"):
            put("code", [ln[1:]], True)
            continue
        s = ln.strip()
        if not s:
            if out and out[-1][0] not in ("list",):         # 列表项之间的空行不打断列表
                fresh[0] = True
            continue
        if _RULE.match(ln):
            fresh[0] = True
            continue
        if (h := HEADING_RX.match(s)):
            put("head", [f"{h.group(1)} {h.group(2).strip('*').strip()}"], False)
        elif (b := _BOLD_LINE.match(s)) and not _is_sentence(b.group(1)):
            put("head", [f"## {b.group(1).strip()}"], False)
        elif (b := _BRACKET_LINE.match(s)):
            put("head", [f"## {b.group(1).strip()}"], False)
        elif _TABLE.match(ln):
            put("table", [s], True)
        elif s.startswith(">"):
            put("quote", [s], True)
        elif (r := REVIEW_RX.match(ln)):
            put("list", _review(r), True)
        elif (it := _ITEM.match(ln)) or (it := _DOT_ITEM.match(ln)):
            indent = len(it.group(1).expandtabs(4))
            num = it.group(2) if it.re is _ITEM else None
            body = it.group(it.re.groups)
            put("list", [(indent, f"{num}." if num else "-", body)], True)
        elif out and out[-1][0] == "list" and not fresh[0] and ln[:2] in ("  ", "\t") and ln.lstrip() != ln \
                and isinstance(out[-1][1][-1], tuple) and len(ln) - len(ln.lstrip()) > out[-1][1][-1][0]:
            ind, mark, body = out[-1][1][-1]                # 列表项的续行
            out[-1][1][-1] = (ind, mark, f"{body} {s}")
        else:
            put("para", [s], False)
        fresh[0] = False
    return out


def _list_lines(items: list) -> list[str]:
    """列表项缩进归一：按出现的缩进排级，每级两个空格。审核条目已是成品行。"""
    levels: list[int] = []
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
            levels = []
            continue
        ind, mark, body = it
        while levels and levels[-1] > ind:
            levels.pop()
        if not levels or levels[-1] < ind:
            levels.append(ind)
        out.append("  " * (len(levels) - 1) + f"{mark} {body}")
    return out


def normalize(text: str) -> str:
    """任何 Markdown → 规整 Markdown：块与块之间恰有一个空行，相邻的普通行各成一段。"""
    parts = []
    for kind, lines in blocks(text):
        parts.append("\n".join(_list_lines(lines) if kind == "list" else lines))
    return "\n\n".join(parts) + "\n" if parts else ""
