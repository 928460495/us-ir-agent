"""第三次调用 ③ --assume 的美股适配（用户 2026-10-26 批准的两项）：状态显示名与 ② 一致、
审核者提示写 10-K 基期。A 股提示词不变。不联网。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

from ir_agent import assume, gaps
from ir_agent.methodology import parse as parse_meth

AS_OF = date(2026, 10, 26)
MODEL = parse_meth("""模式: 变化
因子: 出货 | 全球智能手机出货变化 | 变化率 | 机构 | smartphone shipments
因子: 服务 | 服务营收变化 | 变化率 | 10-K | Services net sales
因子: 毛利 | 产品毛利变动 | pp | 8-K | Products gross margin
公式: 营收 = 上期(营收) × (1 + 出货) × (1 + 服务)
公式: 毛利率 = 基期(毛利率) + 毛利 / 100
""").model


def _fd(status):
    f = MODEL.factors[0]
    return {f.key: gaps.FactorData(factor=f, status=status, note="Counterpoint")}


def test_美股判断项提示词里的状态名与第二次调用一致():
    us = assume.build_prompt({}, ["2026E"], "", decomps=MODEL, factor_data=_fd(gaps.REPORT), us=True)
    cn = assume.build_prompt({}, ["2026E"], "", decomps=MODEL, factor_data=_fd(gaps.REPORT))
    assert "机构测算" in us and "研报测算" not in us and "10-K 披露" in us and "年报披露" not in us
    assert "研报测算" in cn and "年报披露" in cn


def test_美股依据核对报错用美股状态名():
    fd = _fd(gaps.REPORT)[MODEL.factors[0].key]
    _b, prob_us = assume._factor_basis("无数据（估计）", fd, AS_OF, us=True)
    _b, prob_cn = assume._factor_basis("无数据（估计）", fd, AS_OF)
    assert "机构测算" in prob_us and "研报测算" not in prob_us
    assert "研报测算" in prob_cn


def test_审核者首年锚定说明_美股写10K基期():
    from ir_agent.assume_review import _anchor_lines
    anchor = {"fy_rev": 110.0, "fy_cogs": 60.0, "base_rev": 100.0, "k": 3, "g_rev": 0.1, "g_cogs": 0.05}
    mb = {k: [D("0.45"), D("0.46")] for k in ("bear", "base", "bull")}
    us = "\n".join(_anchor_lines(SimpleNamespace(code="AAPL", margin_bases=mb), anchor))
    cn = "\n".join(_anchor_lines(SimpleNamespace(code="600183", margin_bases=mb), anchor))
    assert "10-K 基期" in us and "年报基期" not in us
    assert "年报基期" in cn
