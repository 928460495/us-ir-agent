"""收入地域加权 ERP。PLTR 2026-11-16 实跑：FMP 地区收入里混进「Geographic Concentration Risk」（等于营收
合计），真实地区权重全被压半；未映射行国别 CRP 写成空，工作簿公式与 Python 的 ERP 不一致，验算器崩。不联网。"""

import us_data


def test_剔除集中度风险等合计项():
    w = us_data._geo_weights({"Geographic Concentration Risk": 100.0, "UNITED STATES": 74.0,
                              "Rest Of World": 16.0, "UNITED KINGDOM": 10.0})
    assert set(w) == {"UNITED STATES", "Rest Of World", "UNITED KINGDOM"}
    assert abs(w["UNITED STATES"] - 0.74) < 1e-9


def test_金额等于其余之和的成员按合计项剔除():
    w = us_data._geo_weights({"Worldwide": 200.0, "Americas": 120.0, "Europe": 80.0})
    assert set(w) == {"Americas", "Europe"}


def test_正常地区不受影响():
    w = us_data._geo_weights({"Americas": 120.0, "Europe": 80.0, "Greater China": 60.0})
    assert len(w) == 3 and abs(sum(w.values()) - 1) < 1e-9


def test_未映射行写入补齐用的CRP_工作簿公式与ERP一致(monkeypatch):
    monkeypatch.setattr(us_data, "country_risk_premiums", lambda: dict(
        us_crp=0.0023, by_country={"UNITED STATES": 0.0023, "UNITED KINGDOM": 0.0078},
        by_region={}, exus_crp=0.021, source="test"))
    monkeypatch.setattr(us_data, "revenue_geography", lambda t: dict(
        fy=2025, weights={"UNITED STATES": 0.7, "UNITED KINGDOM": 0.1, "Atlantis": 0.2}))
    g = us_data.geo_weighted_erp("X", 0.04)
    assert all(r["crp"] is not None for r in g["rows"])
    unm = next(r for r in g["rows"] if r["segment"] == "Atlantis")
    assert "未映射" in unm["mapped"]
    assert abs(g["base"] + sum(r["weight"] * r["crp"] for r in g["rows"]) - g["erp"]) < 1e-4
