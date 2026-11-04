"""日本企業の財務データ（J-Quants /v2/fins/summary）のテスト。

レコードの形は公式仕様（すべて文字列、記載のない項目は空文字列）と 2026-11-04 の実際の応答に合わせた。値はテスト用。
"""
from datetime import date, datetime, timezone

import pytest

from app.db import SessionLocal
from app.models import DataQualityCheck, FinancialFact, JpDisclosure, Security
from app.providers import jquants as J
from app.services import ingest_jquants_fins as ING
from app.services.financials import snapshot

FIELDS = ["Sales", "OP", "OdP", "NP", "EPS", "DEPS", "TA", "Eq", "EqAR", "BPS", "CFO", "CFI", "CFF", "CashEq",
          "DivAnn", "ShOutFY", "TrShFY", "AvgSh", "FSales", "NxFSales"]


def rec(disc_no, code, disc_date, doc_type, per, per_st, per_en, fy_st, fy_en, disc_time="15:00:00", **vals):
    r = {"DiscDate": disc_date, "DiscTime": disc_time, "Code": code, "DiscNo": disc_no, "DocType": doc_type,
         "CurPerType": per, "CurPerSt": per_st, "CurPerEn": per_en, "CurFYSt": fy_st, "CurFYEn": fy_en,
         "NxtFYSt": "", "NxtFYEn": ""}
    r.update({f: "" for f in FIELDS})
    r.update({k: str(v) for k, v in vals.items()})
    return r


def test_doc_type_and_numbers():
    assert J.parse_doc_type("FYFinancialStatements_Consolidated_IFRS") == {"kind": "statement", "consolidated": True, "standard": "IFRS"}
    assert J.parse_doc_type("1QFinancialStatements_NonConsolidated_JP")["standard"] == "JGAAP"
    assert J.parse_doc_type("2QFinancialStatements_Consolidated_US")["standard"] == "USGAAP"
    assert J.parse_doc_type("OtherPeriodFinancialStatements_Consolidated_JMIS")["standard"] == "JMIS"
    assert J.parse_doc_type("EarnForecastRevision")["kind"] == "forecast"
    assert J.parse_doc_type("SomethingNew") is None
    assert J.to_number("") is None and J.to_number("-330000000") == -330000000 and J.to_number("0.364") == 0.364
    with pytest.raises(ValueError):
        J.to_number("－")
    assert J.from_jq_code("72030") == "7203" and J.from_jq_code("167A0") == "167A"


def test_parse_record_units_periods_and_blanks():
    r = rec("1", "72030", "2026-08-04", "1QFinancialStatements_Consolidated_IFRS", "1Q", "2026-04-01", "2026-06-30",
            "2026-04-01", "2027-03-31", Sales=100, OP=10, NP=8, EPS="1.5", TA=500, Eq=200, EqAR="0.364", ShOutFY=50,
            DivAnn=9, FSales=999)
    p = J.parse_fin_record(r)
    got = {f["concept"]: f for f in p["facts"]}
    assert p["disclosure"]["accounting_standard"] == "IFRS" and p["disclosure"]["code"] == "7203"
    assert "ordinary_income" not in got  # IFRS には経常利益の概念がない（空文字列）→ 保存しない
    assert "dividend_per_share" not in got  # 配当実績は通期の書類だけ
    assert got["revenue"]["unit"] == "JPY" and got["revenue"]["period_start"] == date(2026, 4, 1)
    assert got["assets"]["period_start"] is None and got["eps_basic"]["unit"] == "JPY/shares"
    assert got["net_assets"]["tag"] == "jquants:Eq" and got["equity_ratio"]["unit"] == "pure"
    assert got["revenue"]["form"] == "TANSHIN-Q1" and got["revenue"]["fiscal_period"] == "Q1" and got["revenue"]["fiscal_year"] == 2027
    assert all(f["accn"] == "1" and f["filed"] == date(2026, 8, 4) for f in p["facts"])
    assert all(f["concept"] != "revenue" or f["value"] == 100 for f in p["facts"])
    assert len(J.form_code("OtherPeriod")) <= 16
    fc = J.parse_fin_record(rec("2", "72030", "2026-08-04", "EarnForecastRevision", "FY", "2026-04-01", "2027-03-31",
                                "2026-04-01", "2027-03-31", FSales=1))
    assert fc["disclosure"]["kind"] == "forecast" and fc["facts"] == []  # 予想は実績に入れない


def _jp(s, code):
    x = Security(market="JP", code=code, name=code, name_search=code, security_type="common", is_active=True,
                 source="test", fetched_at=datetime.now(timezone.utc))
    s.add(x)
    s.flush()
    return x


def _store(rows):
    with SessionLocal() as s:
        st = ING.store_records(s, rows, ING.jp_security_map(s), datetime.now(timezone.utc))
        s.commit()
        return st


def test_store_is_idempotent_keeps_corrections_and_only_touches_jp(client):
    with SessionLocal() as s:
        jp = _jp(s, "9999")
        us = Security(market="US", code="9999", name="US twin", name_search="x", security_type="common", is_active=True,
                      source="t", fetched_at=datetime.now(timezone.utc))
        s.add(us)
        s.commit()
        jp_id, us_id = jp.id, us.id
    fy = dict(per="FY", per_st="2025-04-01", per_en="2026-03-31", fy_st="2025-04-01", fy_en="2026-03-31")
    rows = [rec("A1", "99990", "2026-05-10", "FYFinancialStatements_Consolidated_JP", disc_time="15:00:00",
                Sales=1000, OP=100, OdP=110, NP=70, TA=5000, Eq=2000, DivAnn=50, **fy),
            rec("ZZ", "12340", "2026-05-10", "FYFinancialStatements_Consolidated_JP", Sales=1, **fy),  # 一覧にない
            rec("P1", "99995", "2026-05-10", "FYFinancialStatements_Consolidated_JP", Sales=1, **fy)]  # 優先株など
    st1 = _store(rows)
    assert st1["disclosures_added"] == 3 and st1["facts_added"] == 7 and len(st1["unmatched"]) == 2
    st2 = _store(rows)  # 同じ応答をもう一度：何も増えない
    assert st2["disclosures_added"] == 0 and st2["facts_added"] == 0 and st2["facts_existing"] == 7
    # 訂正開示（新しい開示番号・同じ開示日の遅い時刻）。番号は小さくても、開示時刻で新しい方を使う
    corr = rec("A0", "99990", "2026-05-10", "FYFinancialStatements_Consolidated_JP", disc_time="17:30:00",
               Sales=1200, OP=100, OdP=110, NP=70, TA=5000, Eq=2000, **fy)
    _store([corr])
    with SessionLocal() as s:
        assert s.query(FinancialFact).filter_by(security_id=us_id).count() == 0  # 米国株には書き込まない
        assert s.query(FinancialFact).filter_by(security_id=jp_id, concept="revenue").count() == 2  # 訂正前も残る
        assert s.query(JpDisclosure).count() == 4
        snap = snapshot(s, s.get(Security, jp_id))
        assert snap["annual"][-1]["values"]["revenue"] == 1200
        assert snap["annual"][-1]["values"]["ordinary_income"] == 110 and snap["currency"] == "JPY"
        assert snap["balance"]["values"]["net_assets"] == 2000
        # 訂正の前の時点では、訂正前の値（時点つき）
        assert snapshot(s, s.get(Security, jp_id), as_of=date(2026, 5, 9))["annual"] == []
    # 古い決算の開示を後から取得しても、新しい期の値を置き換えない
    old = rec("B1", "99990", "2025-05-12", "FYFinancialStatements_Consolidated_JP", per="FY", per_st="2024-04-01",
              per_en="2025-03-31", fy_st="2024-04-01", fy_en="2025-03-31", Sales=900, TA=4000, Eq=1800)
    _store([old])
    with SessionLocal() as s:
        snap = snapshot(s, s.get(Security, jp_id))
        assert [a["period_end"] for a in snap["annual"]] == ["2025-03-31", "2026-03-31"]
        assert snap["annual"][-1]["values"]["revenue"] == 1200 and snap["balance"]["values"]["assets"] == 5000
    r = client.get("/api/v1/securities/JP/9999/financials").json()
    assert r["currency"] == "JPY" and r["annual"][-1]["values"]["revenue"] == 1200


def test_jp_ttm_uses_prior_year_same_quarter(client):
    with SessionLocal() as s:
        _jp(s, "8888")
        s.commit()
    T = "FinancialStatements_Consolidated_JP"
    _store([
        rec("F25", "88880", "2026-05-10", "FY" + T, "FY", "2025-04-01", "2026-03-31", "2025-04-01", "2026-03-31", Sales=1000, NP=100, TA=1),
        rec("Q25", "88880", "2025-08-05", "1Q" + T, "1Q", "2025-04-01", "2025-06-30", "2025-04-01", "2026-03-31", Sales=240, NP=20, TA=1),
        rec("Q26", "88880", "2026-08-05", "1Q" + T, "1Q", "2026-04-01", "2026-06-30", "2026-04-01", "2027-03-31", Sales=300, NP=35, TA=1),
    ])
    with SessionLocal() as s:
        t = snapshot(s, s.scalar(__import__("sqlalchemy").select(Security).where(Security.code == "8888")))["ttm"]
    assert t["values"]["revenue"] == 1000 + 300 - 240 and t["values"]["net_income"] == 100 + 35 - 20


def test_run_with_fake_client_records_quality_and_skips_without_key(client, monkeypatch):
    from app import config
    r = ING.run(SessionLocal(), codes=["7203"])
    assert r.status == "skipped"  # キーなし（テストではキー未設定）
    monkeypatch.setenv("NETWORK_SECRETS", "jquants")
    config.get_settings.cache_clear()
    with SessionLocal() as s:
        _jp(s, "7777")
        s.commit()

    class Fake:
        def __init__(self, *a, **k):
            pass

        def fins_summary(self, code=None, disc_date=None):
            if code == "6666":
                raise RuntimeError("HTTP 500")
            return [rec("X1", "77770", "2026-05-10", "FYFinancialStatements_Consolidated_JP", "FY", "2025-04-01",
                        "2026-03-31", "2025-04-01", "2026-03-31", Sales="abc", OP=5)]

        def close(self):
            pass

    monkeypatch.setattr(J, "JQuantsClient", Fake)
    r = ING.run(SessionLocal(), codes=["7777", "6666"])
    assert r.status == "partial" and r.rows == 1 and "6666" in r.message  # 1社の失敗で他を止めない
    with SessionLocal() as s:
        q = s.query(DataQualityCheck).filter_by(dataset="jquants_fins", check_type="invalid_value").one()
        assert q.detail["field"] == "Sales"  # 数値でない値は保存せず記録する
    config.get_settings.cache_clear()


def test_window_dates_respects_free_plan_delay_and_skips_fetched():
    d = ING.window_dates(date(2026, 11, 4), "free", {"2026-08-12"}, 3)
    assert d[0] <= date(2026, 8, 12) and date(2026, 8, 12) not in d and all(x.weekday() < 5 for x in d)


def test_ttm_not_computed_across_accounting_standard_change(client):
    """東京海上（8766）のように、期の途中で日本基準から IFRS に変わった場合、基準をまたいで足し引きしない。"""
    with SessionLocal() as s:
        _jp(s, "5555")
        s.commit()
    _store([
        rec("F", "55550", "2026-05-10", "FYFinancialStatements_Consolidated_JP", "FY", "2025-04-01", "2026-03-31", "2025-04-01", "2026-03-31", Sales=1000, TA=1),
        rec("P", "55550", "2025-08-05", "1QFinancialStatements_Consolidated_JP", "1Q", "2025-04-01", "2025-06-30", "2025-04-01", "2026-03-31", Sales=240, TA=1),
        rec("C", "55550", "2026-08-05", "1QFinancialStatements_Consolidated_IFRS", "1Q", "2026-04-01", "2026-06-30", "2026-04-01", "2027-03-31", Sales=300, TA=1),
    ])
    r = client.get("/api/v1/securities/JP/5555/financials").json()
    assert r["ttm"] is None and r["annual"][-1]["accounting_standard"] == "JGAAP"


def test_jquants_fins_not_scheduled_unless_enabled(monkeypatch):
    """定期実行の worker は、利用者が JQUANTS_FINS_SCHEDULE=true にするまで日本企業の財務情報を取得しない。"""
    from app import config
    from app.worker import schedule_intervals
    monkeypatch.delenv("JQUANTS_FINS_SCHEDULE", raising=False)
    config.get_settings.cache_clear()
    assert "jquants_fins" not in schedule_intervals() and "edinet" not in schedule_intervals()
    monkeypatch.setenv("JQUANTS_FINS_SCHEDULE", "true")
    config.get_settings.cache_clear()
    assert schedule_intervals()["jquants_fins"] == 24 * 3600
    config.get_settings.cache_clear()
