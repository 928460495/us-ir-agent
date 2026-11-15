"""美股行业监管机构：按 FF49 大类给出机构名与官网域名，写进开放网络检索提示作为「优先来源」。

只做提示（用户 2026-11-15 决定：openFDA、FDIC 结构化接口本次不做）。域名命中 openweb 的
`_OFFICIAL_HOSTS` 与否不在这里决定 —— 分级仍按代码抓取后的最终网址。

sec.gov 不列：开放网络跳过 sec.gov（申报文件由 EDGAR 带身份请求头入库）。
"""

from __future__ import annotations

# 所有行业都适用：反垄断与消费者保护、司法部（诉讼、和解、刑事）
_ALL: tuple[tuple[str, str], ...] = (("FTC", "ftc.gov"), ("DOJ", "justice.gov"))

_HEALTH = (("FDA", "fda.gov"), ("CMS", "cms.gov"))
_FIN = (("Federal Reserve", "federalreserve.gov"), ("FDIC", "fdic.gov"), ("OCC", "occ.gov"))
_TRANSPORT = (("FAA", "faa.gov"), ("DOT", "transportation.gov"))
_ENERGY = (("EIA", "eia.gov"), ("FERC", "ferc.gov"))
_FCC = (("FCC", "fcc.gov"),)
_DEFENSE = (("DoD", "defense.gov"),)
_TECH = (("Commerce BIS", "bis.gov"), ("FCC", "fcc.gov"))

# FF49 编号 → 追加机构（编号见 ir_agent/data/ff49_siccodes.txt）
_BY_FF49: dict[int, tuple[tuple[str, str], ...]] = {
    **{n: _HEALTH for n in (11, 12, 13)},      # Hlth、MedEq、Drugs
    **{n: _FIN for n in (45, 46, 48)},         # Banks、Insur、Fin
    **{n: _TRANSPORT for n in (24, 41)},       # Aero、Trans
    **{n: _ENERGY for n in (29, 30, 31)},      # Coal、Oil、Util
    32: _FCC,                                  # Telcm
    26: _DEFENSE,                              # Guns（国防）
    **{n: _TECH for n in (35, 36, 37)},        # Hardw、Softw、Chips
}


def for_ff49(num: int) -> list[tuple[str, str]]:
    """(机构名, 域名)；通用两家在前，行业专属在后，去重保序。"""
    out: list[tuple[str, str]] = []
    for item in _ALL + _BY_FF49.get(num, ()):
        if item not in out:
            out.append(item)
    return out


def render(num: int | None) -> str:
    """提示词里的写法：「FTC（ftc.gov）、DOJ（justice.gov）、…」；编号取不到给空串。"""
    if num is None:
        return ""
    return "、".join(f"{name}（{dom}）" for name, dom in for_ff49(num))
