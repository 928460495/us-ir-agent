"""USAspending.gov：近 12 个月联邦合同义务金额（OFFICIAL，用户 2026-11-15 决定）。

官方接口、免费、无需 key；请求不带任何身份信息。取的是 obligation（当期义务金额）合计，不是
`Award Amount`（合同累计总额，跨年度，会把十年期合同的总价算进今年）。

**名称匹配**：接口按承接方名称做模糊检索，会带出撞名的无关实体（「Apple Hospitality REIT」之于
Apple Inc.）与近似名（「PALANTIRX LLC」）。只收：首词（核心词）与公司名一致、且其余每个词都出现在
公司名里或属于常见法人 / 政府业务后缀的承接方。纳入的逐个写进说明，没纳入的也列出供人复核。

**时点**：接口只给当前数据。分析时点早于今天时，结果含其后补报的合同动作（国防部有约 90 天
披露滞后），说明里写明。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal

import requests

from ir_agent.ledger import Fact, Method

URL = "https://api.usaspending.gov/api/v2/search/spending_by_category/recipient/"
TIMEOUT = 30
KEY = "gov.contract_obligations_ttm"
CONTRACT_TYPES = ["A", "B", "C", "D"]          # 合同类：BPA Call、PO、Delivery Order、Definitive Contract
LIMIT = 50

_SUFFIX = {"INC", "CORP", "CORPORATION", "CO", "LTD", "HOLDINGS", "PLC", "THE"}
_ALLOWED_TAIL = {"INC", "CORP", "CORPORATION", "CO", "LLC", "LP", "USG", "FEDERAL", "GOVERNMENT",
                 "SOLUTIONS", "SERVICES", "OPERATIONS"}


def _tokens(name: str) -> list[str]:
    return re.sub(r"[^A-Z0-9 ]+", " ", name.upper()).split()


def core_words(company: str) -> list[str]:
    """去掉法人后缀与标点后的第一个词；不足 4 个字母取前两个词（「SAP SE」）。"""
    words = [w for w in _tokens(company) if w not in _SUFFIX]
    if not words:
        return []
    return words[:2] if len(words[0]) < 4 else words[:1]


def _matches(recipient: str, core: list[str], company_words: set[str]) -> bool:
    toks = _tokens(recipient)
    if toks[:len(core)] != core:
        return False
    return all(t in company_words or t in _ALLOWED_TAIL for t in toks[len(core):])


def fetch_contracts(company: str, as_of: date, store) -> tuple[list[Fact], list[str]]:
    """→ ([gov.contract_obligations_ttm], 说明)。接口失败返回 ([], [说明])，不抛。"""
    core = core_words(company)
    if not core:
        return [], [f"USAspending 未查（公司名「{company}」取不出核心词）"]
    start = as_of - timedelta(days=365)
    body = {"filters": {"recipient_search_text": [" ".join(core)],
                        "award_type_codes": CONTRACT_TYPES,
                        "time_period": [{"start_date": start.isoformat(), "end_date": as_of.isoformat()}]},
            "limit": LIMIT, "page": 1}
    try:
        r = requests.post(URL, json=body, timeout=TIMEOUT)
    except requests.RequestException as e:
        return [], [f"USAspending 联邦合同不可用（网络错误 {e.__class__.__name__}）"]
    if r.status_code != 200:
        return [], [f"USAspending 联邦合同不可用（HTTP {r.status_code}）"]
    try:
        payload = r.json()
        rows = list(payload.get("results") or [])
    except (ValueError, AttributeError):
        return [], ["USAspending 联邦合同不可用（响应不是预期的 JSON）"]
    sid = store.save(source="usaspending_recipient", payload=payload, url=URL,
                     fetched_at=datetime.now(), params=body)

    company_words = set(_tokens(company))
    taken, skipped = [], []
    for row in rows:
        name = str(row.get("name") or "")
        amount = Decimal(str(row.get("amount") or 0))
        (taken if _matches(name, core, company_words) else skipped).append((name, amount))
    total = sum((a for _, a in taken), Decimal(0))

    window = f"{start.isoformat()}～{as_of.isoformat()}"
    if taken:
        listed = "；".join(f"{n} {a:,.2f}" for n, a in taken)
        notes = [f"USAspending 近 12 个月联邦合同义务金额合计 {total:,.2f} 美元（{window}；纳入：{listed}）"]
    else:
        notes = [f"USAspending：近 12 个月无联邦合同（{window}，按「{' '.join(core)}」检索，名称相符的承接方为零）"]
    if skipped:
        notes.append("USAspending 名称相近但未纳入：" + "、".join(n for n, _ in skipped[:10]))
    if as_of < date.today():
        notes.append("USAspending 只给当前数据：分析时点早于今天，金额含其后补报的合同动作")
    fact = Fact(key=KEY, value=total, unit="美元", period=as_of.isoformat(), as_of=as_of,
                source_id=sid, method=Method.REPORTED, currency="USD")
    return [fact], notes
