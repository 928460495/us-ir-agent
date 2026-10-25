import builtins
from datetime import date

import pytest

from ir_agent.sources import transcripts
from ir_agent.sources.external import Tier
from ir_agent.sources.snapshot import SnapshotStore


def _row(fy, fq, rd, words="Thank you"):
    return {"symbol": "AAPL", "fiscal_year": fy, "fiscal_quarter": fq, "report_date": rd,
            "transcripts": [{"paragraph_number": 1, "speaker": "Tim Cook", "content": f"{words} Q{fq}."},
                            {"paragraph_number": 2, "speaker": "Analyst", "content": "A question."}]}


ROWS = [_row(2026, 3, "2026-07-30"), _row(2026, 2, "2026-04-30"), _row(2026, 1, "2026-01-29"),
        _row(2026, 4, "2026-10-29")]


@pytest.fixture
def store(tmp_path):
    return SnapshotStore(tmp_path)


def test_只取as_of之前最近两篇且按日期倒序(monkeypatch, store):
    monkeypatch.setattr(transcripts, "_query", lambda symbol: ROWS)
    docs, err = transcripts.fetch_transcripts("AAPL", date(2026, 8, 15), store)
    assert err == "" and [d.published for d in docs] == [date(2026, 7, 30), date(2026, 4, 30)]
    assert docs[0].title == "FY2026 Q3 业绩电话会纪要"
    assert docs[0].doc_id == "defeatbeta_AAPL_2026Q3"


def test_分级与出处(monkeypatch, store):
    monkeypatch.setattr(transcripts, "_query", lambda symbol: ROWS)
    d = transcripts.fetch_transcripts("AAPL", date(2026, 8, 15), store)[0][0]
    assert d.tier is Tier.COMPANY and "二手" in d.publisher and d.prompt_limit == 8000
    assert d.text.startswith("Tim Cook: Thank you Q3.\nAnalyst: A question.")


def test_取回的行落快照(monkeypatch, tmp_path):
    monkeypatch.setattr(transcripts, "_query", lambda symbol: ROWS)
    transcripts.fetch_transcripts("AAPL", date(2026, 8, 15), SnapshotStore(tmp_path))
    assert any(p.name.startswith("defeatbeta_transcripts") for p in tmp_path.iterdir())


def test_查询失败时降级为说明(monkeypatch, store):
    def boom(symbol):
        raise OSError("HTTP 503")
    monkeypatch.setattr(transcripts, "_query", boom)
    docs, err = transcripts.fetch_transcripts("AAPL", date(2026, 8, 15), store)
    assert docs == [] and "电话会纪要" in err and "OSError" in err


def test_没装duckdb时降级为说明(monkeypatch, store):
    real = builtins.__import__

    def no_duckdb(name, *a, **k):
        if name == "duckdb":
            raise ImportError("No module named 'duckdb'")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_duckdb)
    docs, err = transcripts.fetch_transcripts("AAPL", date(2026, 8, 15), store)
    assert docs == [] and "duckdb" in err


def test_代码里的点号换成连字符查(monkeypatch, store):
    seen = []
    monkeypatch.setattr(transcripts, "_query", lambda symbol: seen.append(symbol) or [])
    transcripts.fetch_transcripts("brk.b", date(2026, 8, 15), store)
    assert seen == ["BRK-B"]
