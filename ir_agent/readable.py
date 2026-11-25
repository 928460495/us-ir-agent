"""定性产物的只读阅读版（用户 2026-11-22 选方案 A）。

`macro_view.md`、`strategy.md`、`value_chain.md` 是**可编辑的源文件**：门① 的人改稿签名后程序读回
（按指纹判断是否改过），后续撰写引用的也是其中的占位符。把数字直接写进去，人一改，数字就与占位符
脱钩，裸数字审计也会把它们当成账本外数字。所以数字只进另一份文件：每次运行用账本把源文件渲染成
`<名>_阅读版.md`（占位符 → 数值 + 脚注），程序从不读回它。看板的定性部分用 `render_inline`。

与研报的 `citation.render` 同一套取值与格式（`format_value`），区别只在：取不到的占位符**不抛**，原样
保留并标「账本未取到」—— 阅读版是给人看的，不能因为一处引用缺数就整份不出。时点守卫照旧：分析时点
之后才披露的数字同样算取不到。
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from ir_agent.citation import REF_RE, format_value
from ir_agent.labels import fact_label, source_label
from ir_agent.ledger import LookAheadError

SOURCES = ("macro_view", "strategy", "value_chain")
LABELS = {"macro_view": "宏观定性", "strategy": "战略分析", "value_chain": "产业链"}
SUFFIX = "_阅读版.md"
_MISSING = "（账本未取到）"


def _lookup(m: re.Match[str], ledger, as_of: date):
    source_id, key, period = m.group(1), m.group(2), m.group(3)
    try:
        fact = ledger.get(key, period, as_of=as_of)
    except (KeyError, LookAheadError):
        return None
    if source_id is not None and fact.source_id != source_id:
        return None
    return fact


# 占位符后面隔这些字就算「紧跟」：模型常写「[[CPI]] 为 3.72%」「[[营收]] 393.75亿」（历史产物里一成多如此）
_NEXT_NUM = re.compile(r"[\s（(]{0,2}(?:为|是|约|仅|即|＝|=)?\s?(-?\d[\d,]*(?:\.\d+)?)")
_ALONE_BEFORE = re.compile(r"(?:^|[。！？；|\n])\s*$")
_ALONE_AFTER = re.compile(r"^\s*(?:$|[。！？；|\n])")


def _same_number(shown: str, other: str) -> bool:
    """「81.72%」与「81.7」算同一个数：按较短那个的小数位四舍五入后相等。"""
    m = re.match(r"-?\d[\d,]*(?:\.\d+)?", shown)
    if not m:
        return False
    a, b = Decimal(m.group(0).replace(",", "")), Decimal(other.replace(",", ""))
    places = min(-a.as_tuple().exponent, -b.as_tuple().exponent)
    if abs(a) < 10 and places < 1:                     # 「1」与「1.04x」不算同一个数
        return a == b
    return round(a, places) == round(b, places)


def _display(m: re.Match[str], fact, text: str) -> str:
    """一处占位符显示成什么（spec 2026-11-25-readable-layout §4.3）。"""
    shown, label = format_value(fact), fact_label(fact.key)
    if not label:
        return shown
    nxt = _NEXT_NUM.match(text[m.end():m.end() + 24])
    if nxt and nxt.start(1) <= 8 and _same_number(shown, nxt.group(1)):
        return f"{label}（{fact.period}）"
    if _ALONE_BEFORE.search(text[:m.start()]) and _ALONE_AFTER.match(text[m.end():]):
        return f"{label} {shown}"
    return shown


def render_inline(text: str, ledger, as_of: date) -> str:
    """占位符 → 数值或中文名，不加脚注。取不到的原样保留并标注。"""
    text = text or ""

    def _sub(m):
        fact = _lookup(m, ledger, as_of)
        return _display(m, fact, text) if fact is not None else m.group(0) + _MISSING
    return REF_RE.sub(_sub, text)


def render_readable(text: str, ledger, as_of: date) -> tuple[str, dict[str, str]]:
    """占位符 → 数值或中文名 + `[^n]`。返回（正文, 脚注号 → 来源说明）；同一引用只编一个号。"""
    text = text or ""
    seen: dict[tuple[str, str, str], str] = {}
    notes: dict[str, str] = {}

    def _sub(m):
        fact = _lookup(m, ledger, as_of)
        if fact is None:
            return m.group(0) + _MISSING
        ident = (fact.source_id, fact.key, fact.period)
        if ident not in seen:
            seen[ident] = str(len(seen) + 1)
            notes[seen[ident]] = note_text(fact.key, fact.period, fact.source_id, fact.as_of, fact.method.value)
        return f"{_display(m, fact, text)}[^{seen[ident]}]"

    return REF_RE.sub(_sub, text), notes


def note_text(key: str, period: str, source_id: str, as_of, method: str) -> str:
    """来源说明：中文在前，内部键与 source_id 在后（反查要用）。"""
    how = {"reported": "披露值", "computed": "计算值"}.get(str(method), str(method))
    name = fact_label(key)
    head = f"{name}（{period}）· " if name else ""
    return (f"{head}来源 {source_label(source_id)} · 披露日 {as_of.isoformat()} · {how}"
            f" · {key}@{period} · {source_id}")


def with_notes(body: str, notes: dict[str, str]) -> str:
    """正文 + 文末「数字出处」。"""
    body = body.rstrip("\n") + "\n"
    if not notes:
        return body
    return body + "\n## 数字出处\n\n" + "\n".join(f"[^{k}]: {v}" for k, v in notes.items()) + "\n"


def write_readables(out: Path | str, ledger, as_of: date) -> list[Path]:
    """为目录里已有的定性源文件各写一份阅读版；源文件已不在的，删掉过时的阅读版。返回写出的文件。"""
    out = Path(out)
    written: list[Path] = []
    for name in SOURCES:
        src, dst = out / f"{name}.md", out / f"{name}{SUFFIX}"
        if not src.exists():
            dst.unlink(missing_ok=True)
            continue
        head = (f"> 只读阅读版：占位符已换成账本数值（分析时点 {as_of.isoformat()}），脚注列出处。"
                f"修改请改 `{name}.md`，本文件每次运行重新生成。\n\n")
        dst.write_text(head + with_notes(*render_readable(src.read_text(encoding="utf-8"), ledger, as_of)),
                       encoding="utf-8")
        written.append(dst)
    return written
