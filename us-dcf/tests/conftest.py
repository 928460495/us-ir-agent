import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_cal(name: str = "cal_AAPL.json") -> dict:
    """读回校准固件（JSON；DataFrame 以 split 形式存，不用 pickle —— 跨 pandas 版本会坏）。"""
    import copy
    import io
    import json
    from pathlib import Path

    import pandas as pd

    def dec(o):
        if isinstance(o, dict):
            if "__df__" in o:
                return pd.read_json(io.StringIO(json.dumps(o["__df__"])), orient="split")
            if "__sr__" in o:
                return pd.read_json(io.StringIO(json.dumps(o["__sr__"])), orient="split", typ="series")
            if o.get("__nan__"):
                return float("nan")
            return {k: dec(v) for k, v in o.items()}
        if isinstance(o, list):
            return [dec(v) for v in o]
        return o

    raw = json.loads((Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8"))
    return copy.deepcopy(dec(raw))
