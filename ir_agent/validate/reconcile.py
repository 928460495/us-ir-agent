"""三表勾稽校验。

设计要点: 缺数据时「跳过」而非「通过」。静默通过会让一份数据残缺的报表
看起来完全健康 —— 这正是审计里最危险的一类假阳性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ir_agent.ledger import FactLedger, LookAheadError

DEFAULT_ABS_TOL = Decimal("0.01")
DEFAULT_REL_TOL = Decimal("0.0005")   # 5bp，容纳财报四舍五入


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str                      # "passed" | "failed" | "skipped" | "n/a"
    diff: Decimal | None = None
    detail: str = ""


@dataclass
class ReconcileReport:
    period: str
    as_of: date
    results: list[CheckResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def checks_run(self) -> int:
        return sum(1 for r in self.results if r.status in ("passed", "failed"))

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def complete(self) -> bool:
        """所有校验都拿到了数据 —— 覆盖率与通过率必须分开看。"""
        return all(r.status != "skipped" for r in self.results)     # 不适用不算缺口

    def summary(self) -> str:
        head = f"{self.period} @ {self.as_of.isoformat()}"
        state = "通过" if self.ok else f"失败 {len(self.failures)} 项"
        cover = "完整" if self.complete else "覆盖不完整"
        na = sum(1 for r in self.results if r.status == "n/a")
        na_note = f"，{na} 项不适用" if na else ""
        lines = [f"三表勾稽 {head}: {state}（{self.checks_run}/{len(self.results) - na} 项已执行，"
                 f"{cover}{na_note}）"]
        for r in self.results:
            mark = {"passed": "✓", "failed": "✗", "skipped": "–", "n/a": "○"}[r.status]
            lines.append(f"  {mark} {r.name}{'  ' + r.detail if r.detail else ''}")
        return "\n".join(lines)


# (名称, 左侧项, 右侧各项)
_IDENTITIES: list[tuple[str, str, tuple[str, ...]]] = [
    ("资产=负债+所有者权益", "total_assets", ("total_liabilities", "total_equity")),
    ("净利润=归母+少数股东损益", "net_profit",
     ("net_profit_attr_parent", "minority_interest_profit")),
    ("毛利=营业收入-营业成本", "gross_profit", ("revenue", "-cost_of_revenue")),
    ("期末现金=期初+净增加额", "cash_end", ("cash_begin", "cash_net_change")),
    ("利润表净利润=现金流量表起点", "net_profit", ("cf_net_profit",)),
]


# 美股：两条恒等式在 XBRL 里左右两边是同一个标签，「通过」是恒等而非核对 —— 标不适用，
# 不能算进已执行（那是假绿灯），也不该报「缺数据」（那会让人去找一个本来就不存在的数）。
_NA_US_NO_NCI = ("未报告非控制性权益（NetIncomeLossAttributableToNoncontrollingInterest），"
                 "净利润即归母净利润，核对恒等")
_NA_US_CF_START = ("US GAAP 现金流量表起点与利润表净利润是同一 XBRL 标签"
                   "（ProfitLoss / NetIncomeLoss），核对恒等")


def _not_applicable(ledger: FactLedger, name: str, period: str, as_of: date) -> str:
    """美股（净利润以美元计）下不适用的恒等式 → 原因；其余返回空串。"""
    try:
        if ledger.get("net_profit", period, as_of=as_of).currency != "USD":
            return ""
    except KeyError:
        return ""
    if name == "利润表净利润=现金流量表起点":
        return _NA_US_CF_START
    if name == "净利润=归母+少数股东损益" and not any(
            k == "minority_interest_profit" for k, _p in ledger._facts):    # noqa: SLF001
        # 报了 ProfitLoss（含非控制性权益）却不单列少数股东损益时两者会不同 —— 那是真缺数，照旧跳过
        try:
            same = (ledger.get("net_profit", period, as_of=as_of).value
                    == ledger.get("net_profit_attr_parent", period, as_of=as_of).value)
        except KeyError:
            same = False
        return _NA_US_NO_NCI if same else ""
    return ""


def _within(diff: Decimal, scale: Decimal, abs_tol: Decimal, rel_tol: Decimal) -> bool:
    return diff <= max(abs_tol, abs(scale) * rel_tol)


def reconcile(
    ledger: FactLedger,
    period: str,
    as_of: date,
    abs_tol: Decimal = DEFAULT_ABS_TOL,
    rel_tol: Decimal = DEFAULT_REL_TOL,
) -> ReconcileReport:
    report = ReconcileReport(period=period, as_of=as_of)

    for name, lhs_key, rhs_keys in _IDENTITIES:
        if (why := _not_applicable(ledger, name, period, as_of)):
            report.results.append(CheckResult(name=name, status="n/a", detail=f"不适用：{why}"))
            continue
        needed = [lhs_key] + [k.lstrip("-") for k in rhs_keys]
        values: dict[str, Decimal] = {}
        missing: list[str] = []

        for key in needed:
            try:
                values[key] = ledger.get(key, period, as_of=as_of).value
            except LookAheadError:
                raise                      # 时点违规必须冒泡，不能降级为 skip
            except KeyError:
                missing.append(key)

        if missing:
            report.results.append(CheckResult(
                name=name, status="skipped",
                detail=f"缺少 {', '.join(missing)}",
            ))
            continue

        lhs = values[lhs_key]
        rhs = sum(
            (-values[k.lstrip("-")] if k.startswith("-") else values[k]
             for k in rhs_keys),
            Decimal(0),
        )
        diff = abs(lhs - rhs)

        if _within(diff, lhs, abs_tol, rel_tol):
            report.results.append(CheckResult(name=name, status="passed", diff=diff))
        else:
            report.results.append(CheckResult(
                name=name, status="failed", diff=diff,
                detail=f"左 {lhs} vs 右 {rhs}，差 {diff}",
            ))

    return report
