"""业绩电话会纪要（defeatbeta 转存自 Yahoo Finance）→ 语料库。

**分级为 COMPANY**（用户 2026-10-25 决定）：电话会是公司自己的表述，管理层对前景的话照
COMPANY 规则不得作为假设依据。但它是**二手转存**，转录可能有误、更新时间不固定 ——
publisher 写明「二手」，读者与模型都看得见。只进语料库，不产出账本事实。

数据是一个 2.28 GB 的 parquet，按 symbol 排序、每 200 行一个数据块。duckdb 远程读时
只拉目标股票所在的块（2026-10-25 实测苹果约 3 秒），不下载整份文件。
"""

from __future__ import annotations

from datetime import date, datetime

from ir_agent.sources.edgar import normalize_ticker
from ir_agent.sources.external import Document, Tier

URL = ("https://huggingface.co/datasets/defeatbeta/yahoo-finance-data/resolve/main/"
       "data/US/stock_earning_call_transcripts.parquet")
PUBLISHER = "defeatbeta（转存自 Yahoo Finance，二手）"
PROMPT_LIMIT = 8000


def _query(symbol: str) -> list[dict]:
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs; LOAD httpfs;")
        cur = con.execute(
            "SELECT symbol, fiscal_year, fiscal_quarter, report_date, transcripts "
            f"FROM read_parquet('{URL}') WHERE symbol = ?", [symbol])
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


def _text(paragraphs) -> str:
    rows = sorted(paragraphs or [], key=lambda p: p.get("paragraph_number") or 0)
    return "\n".join(f"{p.get('speaker') or ''}: {p.get('content') or ''}".strip() for p in rows)


def fetch_transcripts(ticker: str, as_of: date, store, n: int = 2) -> tuple[list[Document], str]:
    """as_of 之前最近 n 篇纪要。取不到返回 ([], 说明)，不抛 —— 纪要是补充，不是主线。"""
    symbol = normalize_ticker(ticker)
    try:
        rows = _query(symbol)
    except ImportError as e:
        return [], f"电话会纪要（未安装 duckdb：{e}）"
    except Exception as e:                                   # noqa: BLE001
        return [], f"电话会纪要（defeatbeta 不可达：{e.__class__.__name__}: {str(e)[:120]}）"
    seen = sorted((r for r in rows if r.get("report_date")
                   and date.fromisoformat(str(r["report_date"])[:10]) <= as_of),
                  key=lambda r: str(r["report_date"]), reverse=True)[:n]
    if not seen:
        return [], ""
    sid = store.save(source="defeatbeta_transcripts", payload=seen, url=URL,
                     fetched_at=datetime.now(), params={"symbol": symbol})
    docs = []
    # 网址片段带上季度：同一次取回的几篇共用一个 parquet 与快照，只写快照编号时网址全同，
    # 第二次调用读回按网址去重会把后几篇当重复丢掉（AAPL ② 实跑：Q2 纪要没读回）
    for r in seen:
        fy, fq = r["fiscal_year"], r["fiscal_quarter"]
        docs.append(Document(
            doc_id=f"defeatbeta_{symbol}_{fy}Q{fq}", url=f"{URL}#{sid}/{symbol}_{fy}Q{fq}", publisher=PUBLISHER,
            tier=Tier.COMPANY, title=f"FY{fy} Q{fq} 业绩电话会纪要",
            published=date.fromisoformat(str(r["report_date"])[:10]), retrieved=as_of,
            text=_text(r.get("transcripts")), prompt_limit=PROMPT_LIMIT, channel="transcript"))
    return docs, ""
