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


def test_interrupted_run_is_marked_and_resume_skips_fetched_dates(client, monkeypatch):
    """強制終了で「実行中」のまま残った記録は、次の実行で「中断」にする。取得済みの開示日は取り直さない。"""
    from app import config
    from app.models import FetchLog, IngestionRun
    monkeypatch.setenv("NETWORK_SECRETS", "jquants")
    config.get_settings.cache_clear()
    seen = []

    class Fake:
        def __init__(self, *a, **k):
            pass

        def fins_summary(self, code=None, disc_date=None):
            seen.append(disc_date)
            return []

        def close(self):
            pass

    monkeypatch.setattr(J, "JQuantsClient", Fake)
    with SessionLocal() as s:
        s.add(IngestionRun(dataset="financials_jp", source="test", status="running"))
        s.add(FetchLog(dataset="financials_jp", key="2026-08-12", status="success", rows=0, fetched_at=datetime.now(timezone.utc)))
        s.commit()
        r = ING.run(s, max_dates=2)
        assert r.status == "success"
        assert s.query(IngestionRun).filter_by(dataset="financials_jp", status="interrupted").count() == 1
    assert date(2026, 8, 12) not in seen and len(seen) == 2
    config.get_settings.cache_clear()


def test_refetching_logged_dates_updates_log_instead_of_failing(client, monkeypatch):
    """取得済みの開示日を --dates で取り直しても、取得済みの記録の重複で失敗しない（実データの再実行で見つけた不具合）。"""
    from app import config
    from app.models import FetchLog
    monkeypatch.setenv("NETWORK_SECRETS", "jquants")
    config.get_settings.cache_clear()
    with SessionLocal() as s:
        _jp(s, "6060")
        s.commit()

    class Fake:
        def __init__(self, *a, **k):
            pass

        def fins_summary(self, code=None, disc_date=None):
            return [rec("R1", "60600", "2026-05-14", "FYFinancialStatements_Consolidated_JP", "FY", "2025-04-01",
                        "2026-03-31", "2025-04-01", "2026-03-31", Sales=10)]

        def close(self):
            pass

    monkeypatch.setattr(J, "JQuantsClient", Fake)
    for _ in range(2):
        with SessionLocal() as s:
            r = ING.run(s, dates=[date(2026, 5, 14)])
            assert r.status == "success"
    with SessionLocal() as s:
        assert s.query(FetchLog).filter_by(key="2026-05-14").count() == 1
        assert s.query(FinancialFact).filter_by(accn="R1").count() == 1
    config.get_settings.cache_clear()


# ---- 年度データは「その期の最新の通期の決算短信1件」だけで作る（2026-11-04、27社の問題の回帰テスト） ----
FYJ, FYI = "FYFinancialStatements_Consolidated_JP", "FYFinancialStatements_Consolidated_IFRS"
FY26 = dict(per="FY", per_st="2025-04-01", per_en="2026-03-31", fy_st="2025-04-01", fy_en="2026-03-31")


def _fresh(code):
    with SessionLocal() as s:
        x = _jp(s, code)
        s.commit()
        return x.id


def _snap(code, **kw):
    from sqlalchemy import select
    with SessionLocal() as s:
        return snapshot(s, s.scalar(select(Security).where(Security.market == "JP", Security.code == code)), **kw)


def test_standard_change_restatement_does_not_mix_jgaap_items(client):
    """東京海上（8766）型：日本基準の決算短信の後に、同じ期を IFRS で作り直した決算短信が出た。
    IFRS の行に、日本基準だけの経常利益や、IFRS の書類にない売上高を混ぜない。"""
    _fresh("8766")
    _store([rec("J", "87660", "2026-05-20", FYJ, disc_time="16:00:00", Sales=8872, OdP=1348, NP=980, TA=31961, Eq=5457, **FY26),
            rec("I", "87660", "2026-06-26", FYI, disc_time="15:30:00", NP=531, TA=33002, Eq=8052, **FY26)])
    row = _snap("8766")["annual"][-1]
    assert row["accounting_standard"] == "IFRS" and row["disclosure"] == "I"
    assert row["values"] == {"net_income": 531, "assets": 33002, "net_assets": 8052}
    assert "ordinary_income" not in row["values"] and "revenue" not in row["values"]  # 「—」


def test_correction_uses_only_corrected_disclosure_and_leaves_blanks(client):
    """訂正開示：訂正後の最新の決算短信を使う。訂正後に記載のない項目は、訂正前の値で補わず「—」にする。"""
    _fresh("1332")
    _store([rec("A", "13320", "2026-05-14", FYJ, disc_time="12:00:00", Sales=1000, NP=70, EPS="2.19", BPS="100", **FY26),
            rec("B", "13320", "2026-05-20", FYJ, disc_time="13:30:00", Sales=1010, NP=72, **FY26)])
    row = _snap("1332")["annual"][-1]
    assert row["disclosure"] == "B" and row["values"]["revenue"] == 1010 and row["values"]["net_income"] == 72
    assert "eps_basic" not in row["values"] and "bps" not in row["values"]  # 訂正前の 2.19・100 は残らない
    # 訂正の前の時点では、訂正前の決算短信の値（時点つき）
    old = _snap("1332", as_of=date(2026, 5, 15))["annual"][-1]
    assert old["disclosure"] == "A" and old["values"]["eps_basic"] == 2.19


def test_fiscal_year_change_period_is_not_annual_and_is_recorded(client):
    """決算期変更（2901 型：2025-04-01〜08-31 の5か月）の通期の決算短信は、年度データにせず、品質の記録に残す。"""
    _fresh("2901")
    _store([rec("F1", "29010", "2025-10-14", FYJ, "FY", "2024-09-01", "2025-08-31", "2024-09-01", "2025-08-31", Sales=1305, CFO=-253),
            rec("F2", "29010", "2025-12-01", FYJ, "FY", "2025-04-01", "2025-08-31", "2025-04-01", "2025-08-31", Sales=500, CFO=-201)])
    row = _snap("2901")["annual"][-1]
    assert row["disclosure"] == "F1" and row["values"]["cfo"] == -253  # 5か月の値を年度として出さない
    with SessionLocal() as s:
        assert ING.record_irregular_periods(s) == 1 and ING.record_irregular_periods(s) == 0  # 重複して記録しない
        q = s.query(DataQualityCheck).filter_by(dataset="jquants_fins", check_type="irregular_period").one()
        assert q.detail["disc_no"] == "F2" and q.detail["days"] == 152


def test_edinet_liabilities_only_from_same_accounting_standard(client):
    """EDINET の負債は、採用した決算短信と同じ会計基準の書類の値だけを使う。"""
    from app.models import EdinetDocument
    sid = _fresh("7777")
    _store([rec("Q", "77770", "2026-05-10", FYI, Sales=100, NP=10, TA=500, Eq=200, **FY26)])
    with SessionLocal() as s:
        for doc, std, val in (("EJ", "JGAAP", 111), ("EI", "IFRS", 300)):
            s.add(EdinetDocument(doc_id=doc, security_id=sid, sec_code="77770", edinet_code="E", filer_name="t", form_code="030000",
                                 doc_description="有報", submitted_at="2026-06-20 15:00", period_end=date(2026, 3, 31),
                                 accounting_standard=std, parse_status="parsed", detail={}, fetched_at=datetime.now(timezone.utc)))
            s.add(FinancialFact(security_id=sid, concept="liabilities", tag="edinet:x:Liabilities", unit="JPY", period_start=None,
                                period_end=date(2026, 3, 31), fiscal_year=2026, fiscal_period="FY", form="EDINET-ASR", accn=doc,
                                filed=date(2026, 6, 20 if std == "IFRS" else 25), value=val, source="EDINET",
                                fetched_at=datetime.now(timezone.utc)))
        s.commit()
    snap = _snap("7777")
    assert snap["annual"][-1]["values"]["liabilities"] == 300  # 日本基準の書類（提出は後）の 111 は使わない
    assert snap["balance"]["values"]["liabilities"] == 300 and snap["balance"]["values"]["assets"] == 500


def test_ttm_uses_single_disclosures(client):
    """直近4四半期：年度・当年の累計・前年同期は、それぞれ1件の決算短信の値だけ。前年同期の訂正では訂正後を使う。"""
    _fresh("5050")
    Q = "1QFinancialStatements_Consolidated_JP"
    _store([rec("FY", "50500", "2026-05-10", FYJ, Sales=1000, NP=100, **FY26),
            rec("P0", "50500", "2025-08-05", Q, "1Q", "2025-04-01", "2025-06-30", "2025-04-01", "2026-03-31", Sales=240, NP=20),
            rec("P1", "50500", "2025-08-20", Q, "1Q", "2025-04-01", "2025-06-30", "2025-04-01", "2026-03-31", Sales=250),  # 訂正（NP 記載なし）
            rec("C", "50500", "2026-08-05", Q, "1Q", "2026-04-01", "2026-06-30", "2026-04-01", "2027-03-31", Sales=300, NP=35)])
    t = _snap("5050")["ttm"]
    assert t["values"] == {"revenue": 1000 + 300 - 250}  # 純利益は前年同期（訂正後）に記載がないため計算しない


def test_reit_six_month_periods_are_not_recorded_as_fiscal_year_change(client):
    _fresh("8951")
    _store([rec("R", "89510", "2026-05-15", "FYFinancialStatements_Consolidated_REIT", "FY", "2025-10-01", "2026-03-31",
                "2025-10-01", "2026-03-31", Sales=100)])
    with SessionLocal() as s:
        assert ING.record_irregular_periods(s) == 0
