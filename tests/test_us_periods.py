from datetime import date

import pytest

from ir_agent.sources.us_periods import fiscal_label


@pytest.mark.parametrize("start,end,fye,label", [
    (date(2024, 9, 29), date(2025, 9, 27), "0927", "2025FY"),    # 苹果
    (date(2024, 9, 29), date(2024, 12, 28), "0927", "2025Q1"),   # 苹果财年第一季
    (date(2024, 9, 29), date(2025, 3, 29), "0927", "2025H1"),
    (date(2024, 9, 29), date(2025, 6, 28), "0927", "2025Q1-Q3"),
    (date(2025, 1, 27), date(2026, 1, 25), "0125", "2026FY"),    # 英伟达 FY2026
    (date(2025, 1, 5),  date(2026, 1, 3),  "0103", "2025FY"),    # 一月初的 52/53 周财年 → 上一年
    (None,              date(2025, 9, 27), "0927", "2025FY"),    # 资产负债表时点数
    (date(2025, 3, 30), date(2025, 6, 28), "0927", None),        # 单独披露的三个月：不入账
])
def test_财年标签(start, end, fye, label):
    assert fiscal_label(start, end, fye) == label


@pytest.mark.parametrize("start,end,fye,label", [
    (date(2025, 1, 27), date(2025, 4, 27), "0125", "2026Q1"),    # 英伟达财年第一季在上一个自然年
    (date(2022, 9, 25), date(2023, 9, 30), "0927", "2023FY"),    # 53 周财年
    (date(2025, 1, 5),  date(2025, 4, 5),  "0103", "2025Q1"),    # 一月初财年末公司的第一季
    (None,              date(2024, 12, 28), "0927", "2025Q1"),   # 一季末的资产负债表
    (None,              date(2025, 12, 31), "1231", "2025FY"),   # 自然年公司
    (date(2025, 1, 1),  date(2025, 6, 30), "1231", "2025H1"),
    (date(2024, 9, 29), date(2024, 11, 30), "0927", None),       # 两个月：不是标准期间
    (None,              date(2024, 11, 15), "0927", None),       # 非季末的时点数
])
def test_财年标签边界(start, end, fye, label):
    assert fiscal_label(start, end, fye) == label


def test_财年末格式错误报错():
    with pytest.raises(ValueError):
        fiscal_label(None, date(2025, 9, 27), "927")
