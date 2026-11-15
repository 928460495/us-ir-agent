"""USAspending.gov：近 12 个月联邦合同义务金额（OFFICIAL，用户 2026-11-15 决定）。

官方接口、免费、无需 key；请求不带任何身份信息。取的是 obligation（当期义务金额）合计，不是
`Award Amount`（合同累计总额，跨年度，会把十年期合同的总价算进今年）。

**名称匹配**：接口按承接方名称做模糊检索，会带出撞名的无关实体（「Apple Hospitality REIT」之于
Apple Inc.）与近似名（「PALANTIRX LLC」）。只收：开头的核心词与公司名一致（泛词开头如 GENERAL
要前两个词都对上）、且其余每个词都出现在公司名里或属于常见法人 / 政府业务后缀的承接方；承接方名称
开头的 THE 去掉。纳入的逐个写进说明，没纳入的也列出供人复核。

**有结果却一个都没纳入时不入账**（2026-11-15 终审）：那更可能是名称规则漏了（THE BOEING COMPANY），
写一条官方级的「零联邦合同」是错误数据。只有接口确实返回空，才入账 0。

**时点**：接口只给当前数据（国防部有约 90 天披露滞后，金额会被补报）。与 FMP 一致预期同一规则：
分析时点早于今天超过 MAX_LAG_DAYS 天就不请求、不入账。确定性层每个阶段都重跑，同一请求复用
第一阶段的快照 —— 金额不随阶段漂移，后续阶段接口超时也不丢事实。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import requests

from ir_agent.ledger import Fact, Method

URL = "https://api.usaspending.gov/api/v2/search/spending_by_category/recipient/"
TIMEOUT = 30
KEY = "gov.contract_obligations_ttm"
CONTRACT_TYPES = ["A", "B", "C", "D"]          # 合同类：BPA Call、PO、Delivery Order、Definitive Contract
LIMIT = 50
MAX_LAG_DAYS = 3
SOURCE = "usaspending_recipient"

_SUFFIX = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "HOLDINGS", "PLC", "THE"}
_ALLOWED_TAIL = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "LLC", "LP", "USG", "FEDERAL",
                 "GOVERNMENT", "SOLUTIONS", "SERVICES", "OPERATIONS"}
# 泛词开头的公司名（General Motors、United Rentals）只比首词会撞上一大片无关承接方
_GENERIC = {"GENERAL", "INTERNATIONAL", "AMERICAN", "UNITED", "NATIONAL", "FIRST", "GLOBAL",
            "ADVANCED", "APPLIED", "NORTHERN", "SOUTHERN", "WESTERN", "EASTERN"}


def _tokens(name: str) -> list[str]:
    toks = re.sub(r"[^A-Z0-9 ]+", " ", name.upper()).split()
    return toks[1:] if toks[:1] == ["THE"] else toks


def core_words(company: str) -> list[str]:
    """去掉法人后缀与标点后的第一个词；不足 4 个字母或是泛词时取前两个词（「SAP SE」「GENERAL MOTORS」）。"""
    words = [w for w in _tokens(company) if w not in _SUFFIX]
    if not words:
        return []
    return words[:2] if len(words[0]) < 4 or words[0] in _GENERIC else words[:1]


def _cache_path(store, body: dict) -> Path:
    key = hashlib.md5(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]
    return Path(store.root) / "_cache" / f"{SOURCE}_{key}.json"


def _cached(store, body: dict) -> tuple[dict, str] | None:
    p = _cache_path(store, body)
    try:
        sid = json.loads(p.read_text(encoding="utf-8"))["source_id"]
        return store.load(sid).payload, sid
    except (OSError, ValueError, KeyError):
        return None


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
    hit = _cached(store, body)
    if hit is not None:
        payload, sid = hit
    else:
        if as_of < date.today() - timedelta(days=MAX_LAG_DAYS):
            return [], [f"USAspending 未入账：分析时点 {as_of} 早于今天超过 {MAX_LAG_DAYS} 天，接口只给"
                        "当前数据（含其后补报的合同动作），拿不到当时可见的金额"]
        try:
            r = requests.post(URL, json=body, timeout=TIMEOUT)
        except requests.RequestException as e:
            return [], [f"USAspending 联邦合同不可用（网络错误 {e.__class__.__name__}）"]
        if r.status_code != 200:
            return [], [f"USAspending 联邦合同不可用（HTTP {r.status_code}）"]
        try:
            payload = r.json()
            payload.get("results")
        except (ValueError, AttributeError):
            return [], ["USAspending 联邦合同不可用（响应不是预期的 JSON）"]
        fetched = datetime.now()
        sid = store.save(source=SOURCE, payload=payload, url=URL, fetched_at=fetched, params=body)
        cp = _cache_path(store, body)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps({"fetched": fetched.isoformat(), "source_id": sid}), encoding="utf-8")
    rows = list(payload.get("results") or [])

    company_words = set(_tokens(company))
    taken, skipped = [], []
    for row in rows:
        name = str(row.get("name") or "")
        amount = Decimal(str(row.get("amount") or 0))
        (taken if _matches(name, core, company_words) else skipped).append((name, amount))
    total = sum((a for _, a in taken), Decimal(0))

    window = f"{start.isoformat()}～{as_of.isoformat()}"
    query = " ".join(core)
    if not taken and skipped:
        return [], [f"USAspending 未入账：按「{query}」检索到的承接方名称都与公司名不符（{window}："
                    + "、".join(n for n, _ in skipped[:10]) + "），可能是名称规则漏认，不能当作零合同"]
    if taken:
        listed = "；".join(f"{n} {a:,.2f}" for n, a in taken)
        notes = [f"USAspending 近 12 个月联邦合同义务金额合计 {total:,.2f} 美元（{window}；纳入：{listed}）"]
    else:
        notes = [f"USAspending：近 12 个月无联邦合同（{window}，按「{query}」检索，接口无结果）"]
    if skipped:
        notes.append("USAspending 名称相近但未纳入：" + "、".join(n for n, _ in skipped[:10]))
    if len(rows) >= LIMIT:
        notes.append(f"USAspending 结果满 {LIMIT} 条，只取了第一页：排在后面的同名承接方可能漏计")
    if as_of < date.today():
        notes.append("USAspending 只给当前数据：分析时点早于今天，金额可能含其后补报的合同动作")
    fact = Fact(key=KEY, value=total, unit="美元", period=as_of.isoformat(), as_of=as_of,
                source_id=sid, method=Method.REPORTED, currency="USD")
    return [fact], notes
