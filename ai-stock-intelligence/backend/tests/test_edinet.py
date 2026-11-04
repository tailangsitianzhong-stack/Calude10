"""EDINET（負債の合計などの補完）のテスト。

CSV の形式は 2026-11-04 に実際の書類（トヨタ・キーエンス・三菱UFJ）で確認したものに合わせた
（ZIP の XBRL_TO_CSV/jpcrp*.csv、UTF-16・タブ区切り、9列）。値はテスト用。
"""
import io
import zipfile
from datetime import date, datetime, timezone

import pytest

from app.db import SessionLocal
from app.models import DataQualityCheck, EdinetDocument, FinancialFact, Security
from app.providers import edinet as E
from app.providers.http import redact
from app.services import ingest_edinet as ING
from app.services.financials import snapshot

HEAD = ["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "単位", "値"]


def package(rows, dei=None, name="XBRL_TO_CSV/jpcrp030000-asr-001_E99999-000_2026-03-31_01_2026-06-20.csv"):
    dei = {"AccountingStandardsDEI": "Japan GAAP", "WhetherConsolidatedFinancialStatementsArePreparedDEI": "true",
           "CurrentFiscalYearStartDateDEI": "2025-04-01", "CurrentFiscalYearEndDateDEI": "2026-03-31",
           "CurrentPeriodEndDateDEI": "2026-03-31", "TypeOfCurrentPeriodDEI": "FY", "SecurityCodeDEI": "99990", **(dei or {})}
    lines = [HEAD] + [[f"jpdei_cor:{k}", "", "FilingDateInstant", "提出日時点", "その他", "時点", "pure", "", v] for k, v in dei.items()]
    lines += rows
    text = "\n".join("\t".join(f'"{c}"' for c in r) for r in lines)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL_TO_CSV/jpaud-aar-cn-001_E99999-000_2026-03-31_01_2026-06-20.csv", "x".encode("utf-16"))
        z.writestr(name, text.encode("utf-16"))
    return buf.getvalue()


def row(el, ctx, val, kind="連結", unit="JPY"):
    return [el, "", ctx, "当期末", kind, "時点", unit, "円", str(val)]


def test_parse_jgaap_ifrs_interim_and_nonconsolidated():
    p = E.parse_csv_package(package([row("jppfs_cor:Liabilities", "CurrentYearInstant", 199),
                                     row("jppfs_cor:Liabilities", "Prior1YearInstant", 180),
                                     row("jppfs_cor:Liabilities", "CurrentYearInstant_NonConsolidatedMember", 5, "個別"),
                                     row("jppfs_cor:Assets", "CurrentYearInstant", 3670),
                                     row("jppfs_cor:NetAssets", "CurrentYearInstant", 3471)]))
    assert p["values"]["liabilities"]["value"] == 199 and p["values"]["liabilities"]["tag"] == "edinet:jppfs_cor:Liabilities"
    assert p["period_end"] == date(2026, 3, 31) and p["accounting_standard"] == "JGAAP" and p["consolidated"]
    # IFRS の連結は「連結・個別」が「その他」。コンテキストIDで判定する
    ifrs = E.parse_csv_package(package([row("jpigp_cor:LiabilitiesIFRS", "InterimInstant", 64, "その他"),
                                        row("jpigp_cor:EquityIFRS", "InterimInstant", 41, "その他")],
                                       {"AccountingStandardsDEI": "IFRS", "CurrentPeriodEndDateDEI": "2025-09-30",
                                        "TypeOfCurrentPeriodDEI": "HY"}))
    assert ifrs["values"]["liabilities"]["value"] == 64 and ifrs["values"]["net_assets"]["value"] == 41
    assert ifrs["accounting_standard"] == "IFRS" and ifrs["period_end"] == date(2025, 9, 30)
    q2 = E.parse_csv_package(package([row("jppfs_cor:Liabilities", "CurrentQuarterInstant", 171)],
                                     {"TypeOfCurrentPeriodDEI": "Q2", "CurrentPeriodEndDateDEI": "2025-09-20"}))
    assert q2["values"]["liabilities"]["context"] == "CurrentQuarterInstant"  # キーエンスの旧様式
    nc = E.parse_csv_package(package([row("jppfs_cor:Liabilities", "CurrentYearInstant_NonConsolidatedMember", 7, "個別")],
                                     {"WhetherConsolidatedFinancialStatementsArePreparedDEI": "false"}))
    assert nc["values"]["liabilities"]["value"] == 7 and not nc["consolidated"]
    usd = E.parse_csv_package(package([row("jppfs_cor:Liabilities", "CurrentYearInstant", 1, unit="USD")]))
    assert "liabilities" not in usd["values"] and usd["problems"][0]["reason"].startswith("単位が円ではない")
    with pytest.raises(ValueError):
        E.parse_csv_package(package([], name="XBRL_TO_CSV/other.csv"))


def test_periodic_documents_filter_and_key_redaction():
    res = [{"docID": "A", "secCode": "72030", "ordinanceCode": "010", "formCode": "030000", "withdrawalStatus": "0"},
           {"docID": "B", "secCode": "72030", "ordinanceCode": "010", "formCode": "030000", "withdrawalStatus": "1"},  # 取下げ
           {"docID": "C", "secCode": "72030", "ordinanceCode": "010", "formCode": "120000", "withdrawalStatus": "0"},  # 対象外
           {"docID": "D", "secCode": None, "ordinanceCode": "010", "formCode": "030000", "withdrawalStatus": "0"},
           {"docID": "F", "secCode": "68610", "ordinanceCode": "010", "formCode": "043A00", "withdrawalStatus": "0"}]
    assert [x["docID"] for x in E.periodic_documents(res, {"72030"})] == ["A"]
    assert [x["docID"] for x in E.periodic_documents(res)] == ["A", "F"]
    assert "SECRETVALUE" not in redact("https://api.edinet-fsa.go.jp/api/v2/documents.json?date=x&Subscription-Key=SECRETVALUE")


class FakeClient:
    """EDINET の代わり。書類一覧と CSV を返す。"""
    pkgs: dict = {}
    listing: dict = {}
    fail: set = set()

    def __init__(self, *a, **k):
        pass

    def documents(self, d):
        return self.listing.get(d, [])

    def csv_package(self, doc_id):
        if doc_id in self.fail:
            raise RuntimeError("HTTP 500 https://api.edinet-fsa.go.jp/api/v2/documents/X?type=5&Subscription-Key=SECRETVALUE")
        return self.pkgs[doc_id]

    def close(self):
        pass


def _setup(monkeypatch):
    from app import config
    monkeypatch.setenv("EDINET_API_KEY", "test-key")
    config.get_settings.cache_clear()
    monkeypatch.setattr(E, "EdinetClient", FakeClient)
    with SessionLocal() as s:
        x = Security(market="JP", code="9999", name="テスト", name_search="test", security_type="common", is_active=True,
                     source="test", fetched_at=datetime.now(timezone.utc))
        s.add(x)
        s.commit()
        return x.id


def _meta(doc, form="030000", submitted="2026-06-20 15:00"):
    return {"docID": doc, "secCode": "99990", "edinetCode": "E99999", "filerName": "テスト", "ordinanceCode": "010",
            "formCode": form, "docDescription": "有価証券報告書", "submitDateTime": submitted, "withdrawalStatus": "0",
            "csvFlag": "1"}


def test_ingest_stores_reported_liabilities_idempotent_and_cross_checks(client, monkeypatch):
    sid = _setup(monkeypatch)
    from app.services.ingest_jquants_fins import store_records
    from tests.test_jp_financials import rec
    with SessionLocal() as s:  # 同じ期末の決算短信（J-Quants）
        store_records(s, [rec("D1", "99990", "2026-05-10", "FYFinancialStatements_Consolidated_JP", "FY", "2025-04-01",
                              "2026-03-31", "2025-04-01", "2026-03-31", Sales=1000, TA=5000, Eq=2000)],
                      {"9999": sid}, datetime.now(timezone.utc))
        s.commit()
    FakeClient.listing = {date(2026, 6, 22): [_meta("S1")]}
    FakeClient.pkgs = {"S1": package([row("jppfs_cor:Liabilities", "CurrentYearInstant", 3000),
                                      row("jppfs_cor:Assets", "CurrentYearInstant", 5000),
                                      row("jppfs_cor:NetAssets", "CurrentYearInstant", 2100)])}  # 純資産だけ食い違う
    FakeClient.fail = set()
    r = ING.run(SessionLocal(), ["9999"], date(2026, 6, 22), date(2026, 6, 22))
    assert r.status == "success" and r.rows == 3
    r2 = ING.run(SessionLocal(), ["9999"], date(2026, 6, 22), date(2026, 6, 22))
    assert r2.rows == 0 and "'documents_skipped_existing': 1" in r2.message  # 重複登録しない
    with SessionLocal() as s:
        assert s.query(FinancialFact).filter_by(security_id=sid, concept="liabilities").count() == 1
        q = s.query(DataQualityCheck).filter_by(dataset="edinet", check_type="inconsistent").one()
        assert q.detail["concept"] == "net_assets"
        snap = snapshot(s, s.get(Security, sid))
        assert snap["balance"]["values"]["liabilities"] == 3000  # 報告された負債の合計
        assert snap["annual"][-1]["values"]["liabilities"] == 3000 and snap["annual"][-1]["values"]["revenue"] == 1000
    api = client.get("/api/v1/securities/JP/9999/financials").json()
    assert api["balance"]["values"]["liabilities"] == 3000


def test_failed_document_is_recorded_and_retried_and_key_not_leaked(client, monkeypatch):
    sid = _setup(monkeypatch)
    FakeClient.listing = {date(2026, 6, 22): [_meta("S2")]}
    FakeClient.pkgs = {"S2": package([row("jppfs_cor:Liabilities", "CurrentYearInstant", 10)])}
    FakeClient.fail = {"S2"}
    r = ING.run(SessionLocal(), ["9999"], date(2026, 6, 22), date(2026, 6, 22))
    assert r.status == "failed" and "SECRETVALUE" not in r.message
    with SessionLocal() as s:
        d = s.get(EdinetDocument, "S2")
        assert d.parse_status == "failed" and "SECRETVALUE" not in str(d.detail)
    FakeClient.fail = set()
    r = ING.run(SessionLocal(), ["9999"], date(2026, 6, 22), date(2026, 6, 22))  # 次の実行で再試行
    assert r.status == "success" and r.rows == 1
    with SessionLocal() as s:
        assert s.get(EdinetDocument, "S2").parse_status == "parsed"


def test_skipped_without_key(client):
    assert ING.run(SessionLocal(), ["7203"], date(2026, 6, 1), date(2026, 6, 2)).status == "skipped"
