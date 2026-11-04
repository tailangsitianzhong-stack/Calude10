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
                              "2026-03-31", "2025-04-01", "2026-03-31", Sales=1000, TA=5_000_000_000, Eq=2_000_000_000)],
                      {"9999": sid}, datetime.now(timezone.utc))
        s.commit()
    FakeClient.listing = {date(2026, 6, 22): [_meta("S1")]}
    FakeClient.pkgs = {"S1": package([row("jppfs_cor:Liabilities", "CurrentYearInstant", 3000),
                                      row("jppfs_cor:Assets", "CurrentYearInstant", 5_000_400_000),     # 百万円未満の差＝丸め
                                      row("jppfs_cor:NetAssets", "CurrentYearInstant", 2_500_000_000)])}  # 5億円の食い違い
    FakeClient.fail = set()
    r = ING.run(SessionLocal(), ["9999"], date(2026, 6, 22), date(2026, 6, 22))
    assert r.status == "success" and r.rows == 3
    r2 = ING.run(SessionLocal(), ["9999"], date(2026, 6, 22), date(2026, 6, 22))
    assert r2.rows == 0 and "'documents_skipped_existing': 1" in r2.message  # 重複登録しない
    with SessionLocal() as s:
        assert s.query(FinancialFact).filter_by(security_id=sid, concept="liabilities").count() == 1
        q = s.query(DataQualityCheck).filter_by(dataset="edinet", check_type="inconsistent").one()
        assert q.detail["concept"] == "net_assets"
        # 両方の出典：決算短信の開示番号と、EDINET の書類管理番号・提出日
        assert q.detail["jquants_disclosure"] == "D1"
        assert q.detail["edinet_doc_id"] == "S1" and q.detail["edinet_submitted_at"] == "2026-06-20 15:00"
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


def test_cross_check_uses_same_standard_rounding_and_records_unavailable_liabilities(client, monkeypatch):
    """実データの検証で見つけた点：
    - 東京海上（8766）：IFRS の有報を、作り直し前の日本基準の決算短信と比べて「不一致」にしていた → 同じ基準とだけ比べる
    - 4263：決算短信は百万円未満を切り捨て（4,218,107,000 → 4,218,000,000）→ 百万円未満の差は「表示単位の丸め」
    - オリックス（8591・米国基準）：EDINET の CSV に負債の合計の要素がない → 品質の記録に残し、値は作らない"""
    sid = _setup(monkeypatch)
    from app.services.ingest_jquants_fins import store_records
    from tests.test_jp_financials import rec
    with SessionLocal() as s:
        store_records(s, [rec("J", "99990", "2026-05-20", "FYFinancialStatements_Consolidated_JP", "FY", "2025-04-01", "2026-03-31",
                              "2025-04-01", "2026-03-31", TA=31_961_000_000, Eq=5_457_000_000),
                          rec("I", "99990", "2026-06-26", "FYFinancialStatements_Consolidated_IFRS", "FY", "2025-04-01", "2026-03-31",
                              "2025-04-01", "2026-03-31", TA=33_002_000_000, Eq=8_052_000_000)], {"9999": sid}, datetime.now(timezone.utc))
        s.commit()
    FakeClient.fail = set()
    FakeClient.listing = {date(2026, 6, 29): [_meta("SI"), _meta("SU", form="043A00")]}
    FakeClient.pkgs = {
        "SI": package([row("jpigp_cor:LiabilitiesIFRS", "CurrentYearInstant", 24_950_000_000, "その他"),
                       row("jpigp_cor:AssetsIFRS", "CurrentYearInstant", 33_002_651_000, "その他"),
                       row("jpigp_cor:EquityIFRS", "CurrentYearInstant", 8_052_371_000, "その他")], {"AccountingStandardsDEI": "IFRS"}),
        "SU": package([row("jpcrp_cor:TotalAssetsUSGAAPSummaryOfBusinessResults", "InterimInstant", 1, "その他")],
                      {"AccountingStandardsDEI": "US GAAP", "CurrentPeriodEndDateDEI": "2025-09-30"}),
    }
    r = ING.run(SessionLocal(), ["9999"], date(2026, 6, 29), date(2026, 6, 29))
    assert r.status == "success"
    with SessionLocal() as s:
        assert s.query(DataQualityCheck).filter_by(dataset="edinet", check_type="inconsistent").count() == 0
        d = s.get(EdinetDocument, "SI").detail["cross_check"]
        assert {c["result"] for c in d} == {"表示単位の丸め"} and {c["jquants_disclosure"] for c in d} == {"I"}
        u = s.query(DataQualityCheck).filter_by(dataset="edinet", check_type="liabilities_unavailable").one()
        assert u.detail["doc_id"] == "SU" and "米国基準" in u.detail["reason"]
        assert s.query(FinancialFact).filter_by(accn="SU").count() == 0  # 値は作らない
        before = s.query(FinancialFact).count()
    # 再照合（API なし）：品質の記録だけを作り直し、財務の値は変えない
    r = ING.run(SessionLocal(), recheck_only=True)
    assert r.status == "success" and "'mismatch': 0" in r.message and "'unavailable': 1" in r.message
    with SessionLocal() as s:
        assert s.query(FinancialFact).count() == before
        assert s.query(DataQualityCheck).filter_by(dataset="edinet", check_type="liabilities_unavailable").count() == 1


def test_failed_fetch_never_blanks_existing_liabilities(client, monkeypatch):
    """EDINET から取得できなかった場合も、既存の負債の値を空欄・ゼロで上書きしない（追記のみ）。"""
    sid = _setup(monkeypatch)
    with SessionLocal() as s:
        s.add(FinancialFact(security_id=sid, concept="liabilities", tag="edinet:jppfs_cor:Liabilities", unit="JPY", period_start=None,
                            period_end=date(2026, 3, 31), fiscal_year=2026, fiscal_period="FY", form="EDINET-ASR", accn="OLD",
                            filed=date(2026, 6, 20), value=3000, source="EDINET", fetched_at=datetime.now(timezone.utc)))
        s.commit()
    FakeClient.listing = {date(2026, 6, 30): [_meta("NEW")]}
    FakeClient.pkgs = {"NEW": b"broken"}
    FakeClient.fail = {"NEW"}
    r = ING.run(SessionLocal(), ["9999"], date(2026, 6, 30), date(2026, 6, 30))
    assert r.status == "failed"
    FakeClient.fail = set()
    FakeClient.pkgs = {"NEW": package([row("jppfs_cor:Assets", "CurrentYearInstant", 5000)])}  # 負債の要素がない書類
    ING.run(SessionLocal(), ["9999"], date(2026, 6, 30), date(2026, 6, 30))
    with SessionLocal() as s:
        vals = [f.value for f in s.query(FinancialFact).filter_by(security_id=sid, concept="liabilities")]
        assert vals == [3000]  # 既存の値はそのまま。空欄・ゼロの行は作らない


# ---- 全銘柄展開のための機能：対象の指定・書類一覧を取得済みの日付・1バッチの上限 ----

def _jp_sec(code, typ="common", active=True):
    with SessionLocal() as s:
        x = Security(market="JP", code=code, name=code, name_search=code, security_type=typ, is_active=active,
                     source="t", fetched_at=datetime.now(timezone.utc))
        s.add(x)
        s.commit()
        return x.id


def _m(doc, sec5, submitted="2026-06-22 15:00"):
    return {**_meta(doc, submitted=submitted), "secCode": sec5}


class Counting(FakeClient):
    lists = 0
    downloads = 0

    def documents(self, d):
        Counting.lists += 1
        return self.listing.get(d, [])

    def csv_package(self, doc_id):
        Counting.downloads += 1
        return super().csv_package(doc_id)


def _setup_batch(monkeypatch):
    from app import config
    monkeypatch.setenv("EDINET_API_KEY", "test-key")
    config.get_settings.cache_clear()
    monkeypatch.setattr(E, "EdinetClient", Counting)
    Counting.lists = Counting.downloads = 0
    Counting.fail = set()
    pkg = package([row("jppfs_cor:Liabilities", "CurrentYearInstant", 10)])
    Counting.pkgs = {d: pkg for d in ("A1", "A2", "A3", "B1", "E1", "R1")}


def test_all_listed_targets_only_active_japanese_common(client, monkeypatch):
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    _jp_sec("167A")                      # 英字を含むコード
    _jp_sec("2222", typ="etf")           # 対象外
    _jp_sec("3333", active=False)        # 上場廃止：対象外
    Counting.listing = {date(2026, 6, 22): [_m("A1", "11110"), _m("A2", "167A0"), _m("E1", "22220"), _m("R1", "33330")]}
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22))
    assert r.status == "success" and "'targets': 2" in r.message and "上限 200 件" in r.source
    with SessionLocal() as s:
        assert {d.doc_id for d in s.query(EdinetDocument)} == {"A1", "A2"}


def test_listed_dates_are_logged_and_not_relisted(client, monkeypatch):
    from app.models import FetchLog
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    Counting.listing = {date(2026, 6, 22): [_m("A1", "11110")], date(2026, 6, 23): [_m("A2", "11110")]}
    ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 23))
    assert Counting.lists == 2
    with SessionLocal() as s:
        assert {l.key for l in s.query(FetchLog).filter_by(dataset="edinet_list")} == {"2026-06-22", "2026-06-23"}
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 23))
    assert Counting.lists == 2 and "'dates_skipped_listed': 2" in r.message  # 2回目は書類一覧を取り直さない
    # 銘柄を指定した実行は、取得済みの日付を記録しない（他の会社の書類を処理していないため）
    ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 24), end=date(2026, 6, 24))
    with SessionLocal() as s:
        assert s.query(FetchLog).filter_by(dataset="edinet_list", key="2026-06-24").count() == 0


def test_max_docs_stops_mid_date_and_resumes(client, monkeypatch):
    from app.models import FetchLog
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    Counting.listing = {date(2026, 6, 22): [_m("A1", "11110"), _m("A2", "11110"), _m("A3", "11110")],
                        date(2026, 6, 23): [_m("B1", "11110")]}
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 23), max_docs=2)
    assert Counting.downloads == 2 and "'limit_reached': True" in r.message
    with SessionLocal() as s:
        assert s.query(FetchLog).filter_by(dataset="edinet_list").count() == 0  # 途中の日は「処理済み」にしない
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 23), max_docs=2)
    assert Counting.downloads == 4  # 残りの A3 と B1 だけ（A1・A2 はスキップ）
    with SessionLocal() as s:
        assert {d.doc_id for d in s.query(EdinetDocument)} == {"A1", "A2", "A3", "B1"}
        assert {l.key for l in s.query(FetchLog).filter_by(dataset="edinet_list")} == {"2026-06-22", "2026-06-23"}
        assert s.query(FinancialFact).filter_by(concept="liabilities").count() == 4  # 重複なし


def test_failed_documents_are_retried_without_relisting(client, monkeypatch):
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    Counting.listing = {date(2026, 6, 22): [_m("A1", "11110")]}
    Counting.fail = {"A1"}
    ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22))
    lists_after_first = Counting.lists
    Counting.fail = set()
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22))
    assert Counting.lists == lists_after_first and "'documents_retried': 1" in r.message  # 一覧は取り直さず再試行
    with SessionLocal() as s:
        assert s.get(EdinetDocument, "A1").parse_status == "parsed"


def test_auth_error_does_not_mark_dates(client, monkeypatch):
    from app.models import FetchLog
    _setup_batch(monkeypatch)
    _jp_sec("1111")

    class Denied(Counting):
        def documents(self, d):
            raise E.EdinetAuthError("EDINET の認証に失敗しました（401）")

    monkeypatch.setattr(E, "EdinetClient", Denied)
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 23))
    assert r.status == "failed"
    with SessionLocal() as s:
        assert s.query(FetchLog).filter_by(dataset="edinet_list").count() == 0


# ---- PR #7 のレビューで追加した確認 ----

def test_auth_error_while_downloading_stops_without_marking_date_or_failing_docs(client, monkeypatch):
    """書類の取得中に 401：その日を「処理済み」にせず、書類も「失敗」と記録しない。キーが直れば次の実行で取得できる。"""
    from app.models import FetchLog
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    Counting.listing = {date(2026, 6, 22): [_m("A1", "11110"), _m("A2", "11110")]}

    class KeyRevoked(Counting):
        def csv_package(self, doc_id):
            raise E.EdinetAuthError("EDINET の認証に失敗しました（401）")

    monkeypatch.setattr(E, "EdinetClient", KeyRevoked)
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22))
    assert r.status == "failed" and "'auth_error': True" in r.message
    with SessionLocal() as s:
        assert s.query(FetchLog).filter_by(dataset="edinet_list").count() == 0
        assert s.query(EdinetDocument).count() == 0  # 認証の失敗を書類の失敗として残さない
    monkeypatch.setattr(E, "EdinetClient", Counting)
    ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22))
    with SessionLocal() as s:
        assert {d.doc_id for d in s.query(EdinetDocument)} == {"A1", "A2"}


def test_max_docs_counts_retries_and_new_documents_together(client, monkeypatch):
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    Counting.listing = {date(2026, 6, 22): [_m("A1", "11110"), _m("A2", "11110")],
                        date(2026, 6, 23): [_m("A3", "11110"), _m("B1", "11110")]}
    Counting.fail = {"A1", "A2"}
    ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22))  # A1・A2 が失敗
    Counting.fail = set()
    Counting.downloads = 0
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 23), max_docs=3)
    assert Counting.downloads == 3 and "'documents_retried': 2" in r.message and "'limit_reached': True" in r.message
    with pytest.raises(ValueError):
        ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22), max_docs=0)


def test_document_failing_on_retry_is_not_tried_twice_in_same_run(client, monkeypatch):
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    Counting.listing = {date(2026, 6, 22): [_m("A1", "11110")]}
    Counting.fail = {"A1"}
    ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 22), end=date(2026, 6, 22))
    Counting.downloads = 0
    ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 22), end=date(2026, 6, 22))  # まだ失敗する
    assert Counting.downloads == 1  # 再試行の1回だけ（同じ実行で書類一覧から2回目は試さない）


def test_other_companies_documents_are_ignored_and_rerun_adds_nothing(client, monkeypatch):
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    Counting.listing = {date(2026, 6, 22): [_m("A1", "11110"), _m("Z9", "99980")]}  # 99980 は銘柄一覧にない
    ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22))
    # 品質の記録（データの問題）だけを数える。実行ごとの要約（dataset=edinet_run）は実行のたびに1件増えるので別に確認する
    quality = lambda s: s.query(DataQualityCheck).filter(DataQualityCheck.dataset != ING.RUN_SUMMARY).count()
    with SessionLocal() as s:
        counts = (s.query(EdinetDocument).count(), s.query(FinancialFact).count(), quality(s))
        assert counts[0] == 1
    ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 22), end=date(2026, 6, 22))  # 同じ日を銘柄指定でも
    with SessionLocal() as s:
        assert (s.query(EdinetDocument).count(), s.query(FinancialFact).count(), quality(s)) == counts
        assert s.query(DataQualityCheck).filter_by(dataset=ING.RUN_SUMMARY).count() == 2


def test_mismatch_record_keeps_edinet_source_on_recheck_and_old_records_stay_valid(client, monkeypatch):
    """不一致の記録に EDINET の書類管理番号・提出日を残す。再照合（API なし）でも同じ項目で作り直す。
    この2項目がない以前の形式の記録があっても、読み取り・再照合が失敗しない。財務の値は変えない。"""
    sid = _setup(monkeypatch)
    from app.services.ingest_jquants_fins import store_records
    from tests.test_jp_financials import rec
    with SessionLocal() as s:
        store_records(s, [rec("D1", "99990", "2026-05-10", "FYFinancialStatements_Consolidated_JP", "FY", "2025-04-01",
                              "2026-03-31", "2025-04-01", "2026-03-31", Sales=1000, TA=5_000_000_000, Eq=2_000_000_000)],
                      {"9999": sid}, datetime.now(timezone.utc))
        s.commit()
    FakeClient.listing = {date(2026, 6, 22): [_meta("S2", submitted="2026-06-22 09:30")]}
    FakeClient.pkgs = {"S2": package([row("jppfs_cor:Liabilities", "CurrentYearInstant", 3000),
                                      row("jppfs_cor:NetAssets", "CurrentYearInstant", 2_500_000_000)])}
    FakeClient.fail = set()
    assert ING.run(SessionLocal(), ["9999"], date(2026, 6, 22), date(2026, 6, 22)).status == "success"
    with SessionLocal() as s:
        # 以前の形式の記録（EDINET の出典の項目なし）を模擬して加える
        s.add(DataQualityCheck(dataset="edinet", subject="9999", check_type="inconsistent", severity="warn",
                               detail={"concept": "assets", "period_end": "2025-03-31", "standard": "JGAAP",
                                       "edinet": 1.0, "jquants": 9_000_000.0, "jquants_disclosure": "OLD"}))
        s.commit()
        old = s.query(DataQualityCheck).filter_by(dataset="edinet", check_type="inconsistent").all()
        assert len(old) == 2 and sum("edinet_doc_id" not in q.detail for q in old) == 1
        facts = [(f.concept, f.value, f.accn) for f in s.query(FinancialFact).order_by(FinancialFact.id)]
    r = ING.run(SessionLocal(), recheck_only=True)
    assert r.status == "success" and "'mismatch': 1" in r.message
    with SessionLocal() as s:
        q = s.query(DataQualityCheck).filter_by(dataset="edinet", check_type="inconsistent").one()  # 作り直し（重複なし）
        assert q.detail["concept"] == "net_assets" and q.detail["jquants_disclosure"] == "D1"
        assert q.detail["edinet_doc_id"] == "S2" and q.detail["edinet_submitted_at"] == "2026-06-22 09:30"
        assert [(f.concept, f.value, f.accn) for f in s.query(FinancialFact).order_by(FinancialFact.id)] == facts
        assert s.get(EdinetDocument, "S2").detail["cross_check"][0]["result"] == "不一致"


# ---- 全銘柄展開の前の追加：失敗の種類の分類・不一致率の監視 ----

def _bs(assets, liabilities=True, standard=None):
    rows = [row("jppfs_cor:Assets", "CurrentYearInstant", assets), row("jppfs_cor:NetAssets", "CurrentYearInstant", 2_000_000_000)]
    if liabilities:
        rows.append(row("jppfs_cor:Liabilities", "CurrentYearInstant", 3_000_000_000))
    return package(rows, {"AccountingStandardsDEI": standard} if standard else None)


def test_failures_are_classified_by_type_and_report_is_read_only(client, monkeypatch):
    """失敗の種類を、エラー文ではなく処理の段階で分類する：API通信（書類の取得）／書類の読み取り／認証（401）。
    負債の要素がない書類・米国基準の書類は、読み取りは成功で、取得の失敗に数えない（種類ごとに数える）。"""
    from app.models import FetchLog, IngestionRun
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    Counting.listing = {date(2026, 6, 22): [_m("OK", "11110"), _m("NET", "11110"), _m("BAD", "11110"),
                                            _m("NOL", "11110"), _m("USG", "11110")]}
    Counting.pkgs = {"OK": _bs(5_000_000_000), "BAD": b"not a zip", "NOL": _bs(5_000_000_000, liabilities=False),
                     "USG": package([row("jpcrp_cor:TotalAssetsUSGAAPSummaryOfBusinessResults", "CurrentYearInstant", 1, "その他")],
                                    {"AccountingStandardsDEI": "US GAAP"})}
    Counting.fail = {"NET"}  # 取得の通信・応答のエラー（HTTP 500）
    r = ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 22), end=date(2026, 6, 22))
    assert r.status == "partial"
    for k, v in {"documents_parsed": 3, "documents_failed": 2, "documents_failed_api": 1, "documents_failed_parse": 1,
                 "documents_failed_store": 0, "auth_error": False,
                 "liabilities_unavailable_no_liabilities_element": 1, "liabilities_unavailable_us_gaap": 1}.items():
        assert f"'{k}': {v!r}" in r.message, k
    assert r.message.startswith("取得の失敗 2（API通信 1・書類の読み取り 1・保存 0）")
    with SessionLocal() as s:
        assert s.get(EdinetDocument, "NET").detail["error_type"] == "api"
        assert s.get(EdinetDocument, "BAD").detail["error_type"] == "parse"
        assert {d.parse_status for d in s.query(EdinetDocument).filter(EdinetDocument.doc_id.in_(["NOL", "USG"]))} == {"parsed"}
        u = {q.detail["doc_id"]: q.detail["reason_type"] for q in
             s.query(DataQualityCheck).filter_by(dataset="edinet", check_type="liabilities_unavailable")}
        assert u == {"NOL": "no_liabilities_element", "USG": "us_gaap"}

    # 認証エラー（401）：書類の失敗にせず、実行を止めて記録する
    class Denied(Counting):
        def csv_package(self, doc_id):
            raise E.EdinetAuthError("EDINET の認証に失敗しました（401）")

    monkeypatch.setattr(E, "EdinetClient", Denied)
    r = ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 22), end=date(2026, 6, 22))
    assert "'auth_error': True" in r.message and "'documents_failed': 0" in r.message

    with SessionLocal() as s:  # 以前の形式の記録（種類の項目なし）
        s.add(EdinetDocument(doc_id="LEG", security_id=None, sec_code="11110", form_code="030000", submitted_at="2026-06-01 15:00",
                             parse_status="failed", detail={"error": "x"}, fetched_at=datetime.now(timezone.utc)))
        s.add(DataQualityCheck(dataset="edinet", subject="1111", check_type="liabilities_unavailable", severity="info",
                               detail={"doc_id": "OLDU", "standard": "USGAAP", "reason": "x"}))
        s.commit()
        counts = {m.__tablename__: s.query(m).count() for m in (EdinetDocument, DataQualityCheck, IngestionRun, FetchLog, FinancialFact)}
        rep = ING.report(s)
        assert not (s.new or s.dirty or s.deleted)
        s.rollback()
        assert counts == {m.__tablename__: s.query(m).count() for m in (EdinetDocument, DataQualityCheck, IngestionRun, FetchLog, FinancialFact)}
    assert rep["documents_failed_by_type"] == {"api": 1, "parse": 1, "store": 0, "unknown": 1}
    assert rep["documents_parsed"] == 3 and rep["documents_failed"] == 3 and rep["failure_rate"] == 0.5
    assert rep["liabilities_unavailable_by_type"] == {"no_liabilities_element": 1, "us_gaap": 2}
    assert rep["auth_error_runs"] == 1


def test_mismatch_threshold_boundaries():
    ex = ING.mismatch_exceeded
    assert not ex(100, 50, 0.10)   # 照合できた書類が100件以下では判定しない
    assert ex(101, 11, 0.10)       # 10.9%
    assert not ex(101, 10, 0.10)   # 9.9%
    assert not ex(200, 20, 0.10)   # ちょうど10%は超えていない
    assert ING.MISMATCH_STOP_RATE == 0.10 and ING.MISMATCH_MIN_DOCS == 100


def _mismatch_setup(monkeypatch, min_docs):
    from app.services.ingest_jquants_fins import store_records
    from tests.test_jp_financials import rec
    _setup_batch(monkeypatch)
    monkeypatch.setattr(ING, "MISMATCH_MIN_DOCS", min_docs)
    sid = _jp_sec("1111")
    with SessionLocal() as s:  # 比べる相手の決算短信（総資産50億円・純資産20億円）
        store_records(s, [rec("D1", "11110", "2026-05-10", "FYFinancialStatements_Consolidated_JP", "FY", "2025-04-01",
                              "2026-03-31", "2025-04-01", "2026-03-31", Sales=1, TA=5_000_000_000, Eq=2_000_000_000)],
                      {"1111": sid}, datetime.now(timezone.utc))
        s.commit()


def _docs(day, spec):
    """spec の文字列：m＝一致する書類、x＝総資産が10億円違う（不一致）書類。"""
    metas = [_m(f"{day.day}{c}{i:03d}", "11110") for i, c in enumerate(spec)]
    Counting.listing[day] = metas
    for meta, c in zip(metas, spec):
        Counting.pkgs[meta["docID"]] = _bs(5_000_000_000 if c == "m" else 6_000_000_000)
    return [x["docID"] for x in metas]


def test_mismatch_rate_stops_run_counts_batch_and_cumulative_and_blocks_next_start(client, monkeypatch):
    """実際の基準（照合できた書類が100件を超えてから、10%超で停止）での動作。"""
    from app.models import FetchLog
    _mismatch_setup(monkeypatch, 100)
    Counting.listing, Counting.pkgs = {}, {}
    ids = _docs(date(2026, 6, 22), "m" * 89 + "x" * 30)
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22), max_docs=500)
    assert r.status == "failed" and r.message.startswith("不一致率の停止基準により自動停止：バッチの不一致率 12/101 件（11.9%）")
    assert "不一致率 バッチ 12/101（11.9%） 累計 12/101（11.9%）" in r.message
    with SessionLocal() as s:
        assert s.query(EdinetDocument).count() == 101          # 101件目で停止。残りは取得しない
        assert s.get(EdinetDocument, ids[101]) is None
        assert s.query(FetchLog).filter_by(dataset="edinet_list").count() == 0  # この日は「処理済み」にしない
        assert s.query(DataQualityCheck).filter_by(dataset="edinet", check_type="inconsistent").count() == 12  # 不一致は総資産のみ
    lists = Counting.lists
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22), max_docs=500)
    assert r.status == "failed" and "開始しない" in r.message and "12/101" in r.message
    assert Counting.lists == lists  # API に接続しない
    # 原因を確認したうえで、再開の指定と基準の変更をして再開すると、残りから続き、日付を記録する
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22), max_docs=500,
                mismatch_stop_rate=0.5, resume_after_mismatch_stop=True)
    assert r.status == "success" and "累計 30/119（25.2%）" in r.message and "バッチ 18/18（100.0%）" in r.message
    with SessionLocal() as s:
        assert s.query(EdinetDocument).count() == 119 and s.query(FetchLog).filter_by(dataset="edinet_list").count() == 1


def test_mismatch_stop_by_batch_rate_only(client, monkeypatch):
    _mismatch_setup(monkeypatch, 4)
    Counting.listing, Counting.pkgs = {}, {}
    _docs(date(2026, 6, 22), "m" * 40)
    assert ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22)).status == "success"
    _docs(date(2026, 6, 23), "mmmmx" + "m" * 5)
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 23), end=date(2026, 6, 23))
    assert "バッチの不一致率 1/5 件（20.0%）" in r.message and "累計 1/45（2.2%）" in r.message
    with SessionLocal() as s:
        assert s.query(EdinetDocument).count() == 45


def test_mismatch_stop_by_cumulative_rate_only(client, monkeypatch):
    _mismatch_setup(monkeypatch, 4)
    Counting.listing, Counting.pkgs = {}, {}
    _docs(date(2026, 6, 22), "mxxx")  # 4件（基準の件数以下なので判定しない）
    assert ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22)).status == "success"
    _docs(date(2026, 6, 23), "mmmm")
    r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 23), end=date(2026, 6, 23))
    assert "累計の不一致率 3/5 件（60.0%）" in r.message and "バッチ 0/1" in r.message
    with SessionLocal() as s:
        assert s.query(EdinetDocument).count() == 5


# ---- PR #9 のレビューの指摘（M1・m1〜m5）への対応 ----

def _summary(s, run_id):
    return s.query(DataQualityCheck).filter_by(dataset=ING.RUN_SUMMARY, subject=str(run_id)).one().detail


def test_batch_stop_blocks_next_run_until_explicit_resume(client, monkeypatch):
    """M1：1回分の不一致率で止まったあと、次の実行は API に接続せずに止まる。専用の指定で再開でき、記録に残る。"""
    from app.models import IngestionRun
    _mismatch_setup(monkeypatch, 4)
    Counting.listing, Counting.pkgs = {}, {}
    _docs(date(2026, 6, 22), "m" * 40)
    assert ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 22), end=date(2026, 6, 22)).status == "success"
    _docs(date(2026, 6, 23), "mmmmx" + "m" * 5)
    r1 = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 23), end=date(2026, 6, 23))
    assert r1.status == "failed" and "バッチの不一致率 1/5 件" in r1.message  # 累計は 1/45（2.2%）で基準以下
    with SessionLocal() as s:
        d1 = _summary(s, r1.id)
        assert d1["mismatch_stop"]["scope"] == "batch" and d1["mismatch_stop"]["compared"] == 5 and d1["status"] == "failed"
    lists, downloads = Counting.lists, Counting.downloads
    # 再開の指定なし：2回続けても、API に接続しない（止まったままにする）
    for _ in range(2):
        r = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 23), end=date(2026, 6, 23))
        assert r.status == "failed" and "開始しない" in r.message and "--resume-after-mismatch-stop" in r.message
        with SessionLocal() as s:
            d = _summary(s, r.id)
            assert d["blocked_before_start"] and d["resume_after_mismatch_stop"] is False and d["mismatch_stop"]["scope"] == "previous_run"
    assert (Counting.lists, Counting.downloads) == (lists, downloads)
    # 再開の指定あり：続きから処理し、指定の有無と直前の停止を記録する
    r3 = ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 23), end=date(2026, 6, 23), resume_after_mismatch_stop=True)
    assert r3.status == "success" and "再開の指定により実行" in r3.message and Counting.lists == lists + 1
    with SessionLocal() as s:
        d3 = _summary(s, r3.id)
        assert d3["resume_after_mismatch_stop"] and d3["resumed_after_mismatch_stop"] and d3["mismatch_stop"] is None
        assert s.query(EdinetDocument).count() == 50
        rep = ING.report(s)
        assert rep["mismatch_stopped_runs"][0] == r1.id and len(rep["mismatch_stopped_runs"]) == 3  # 停止1＋開始しない2
        assert rep["resumed_after_mismatch_stop_runs"] == [r3.id]
    # 再開した実行が止まらずに終われば、次は指定なしで動く
    assert ING.run(SessionLocal(), all_listed=True, start=date(2026, 6, 24), end=date(2026, 6, 24)).status == "success"


def test_auth_error_after_parsed_documents_is_failed_not_partial(client, monkeypatch):
    """m1：認証エラーで止まった実行は、読み取り済みの書類があっても failed。"""
    _setup_batch(monkeypatch)
    _jp_sec("1111")

    class Half(Counting):
        def csv_package(self, doc_id):
            if doc_id == "Z":
                raise E.EdinetAuthError("EDINET の認証に失敗しました（401）")
            return super().csv_package(doc_id)

    monkeypatch.setattr(E, "EdinetClient", Half)
    Counting.listing = {date(2026, 6, 23): [_m("OK1", "11110"), _m("Z", "11110"), _m("OK2", "11110")]}
    Counting.pkgs = {"OK1": _bs(5_000_000_000), "OK2": _bs(5_000_000_000)}
    r = ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 23), end=date(2026, 6, 23))
    assert r.status == "failed" and r.message.startswith("認証エラー（401）で停止") and "'documents_parsed': 1" in r.message
    with SessionLocal() as s:
        d = _summary(s, r.id)
        assert d["auth_error"] is True and d["status"] == "failed" and d["stats"]["documents_parsed"] == 1
        assert s.get(EdinetDocument, "Z") is None and s.get(EdinetDocument, "OK2") is None  # 失敗にせず、続きも処理しない


def test_missing_period_end_is_parse_error(client, monkeypatch):
    """m2：期末日がない書類は「読み取りのエラー」。保存の段階（NOT NULL の違反）に進めない。"""
    with pytest.raises(ValueError, match="期末日"):
        E.parse_csv_package(package([row("jppfs_cor:Liabilities", "CurrentYearInstant", 1)], {"CurrentPeriodEndDateDEI": ""}))
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    Counting.listing = {date(2026, 6, 22): [_m("NOPE", "11110")]}
    Counting.pkgs = {"NOPE": package([row("jppfs_cor:Liabilities", "CurrentYearInstant", 1)], {"CurrentPeriodEndDateDEI": ""})}
    r = ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 22), end=date(2026, 6, 22))
    assert "'documents_failed_parse': 1" in r.message and "'documents_failed_store': 0" in r.message
    with SessionLocal() as s:
        assert s.get(EdinetDocument, "NOPE").detail["error_type"] == "parse"
        assert s.query(FinancialFact).count() == 0


def _client_with(monkeypatch, status_code, body, ctype="application/json"):
    import httpx
    c = E.EdinetClient("dummy")

    def fake_get(url, **kw):
        content = body if isinstance(body, bytes) else __import__("json").dumps(body).encode()
        return httpx.Response(status_code, content=content, headers={"Content-Type": ctype},
                              request=httpx.Request("GET", "https://api.edinet-fsa.go.jp/x"))

    monkeypatch.setattr(c.http, "get", fake_get)
    return c


@pytest.mark.parametrize("body", [{"StatusCode": 401, "message": "Access denied"}, {"statusCode": "401"},
                                  {"metadata": {"status": "401", "message": "Unauthorized"}}])
def test_auth_error_in_json_body_with_http_200(monkeypatch, body):
    """m3：HTTP 200 でも、JSON の本文の状態コードが 401 なら認証エラー（実際の形式は未確認のため、考えられる形を防御的に扱う）。"""
    with pytest.raises(E.EdinetAuthError):
        _client_with(monkeypatch, 200, body).csv_package("S1")
    with pytest.raises(E.EdinetAuthError):
        _client_with(monkeypatch, 200, body).documents(date(2026, 6, 22))


@pytest.mark.parametrize("body", [{"metadata": {"status": "404", "message": "Not Found"}}, {"StatusCode": 500}, {}, b"not json"])
def test_other_json_errors_stay_api_errors(monkeypatch, body):
    with pytest.raises(RuntimeError) as e:
        _client_with(monkeypatch, 200, body).csv_package("S1")
    assert not isinstance(e.value, E.EdinetAuthError)
    assert _client_with(monkeypatch, 200, b"PK\x03\x04zip", "application/octet-stream").csv_package("S1") == b"PK\x03\x04zip"


def test_many_errors_and_auth_are_counted_from_structured_summary(client, monkeypatch):
    """m4：メッセージは1,000文字で切れても、エラー・認証エラー・件数は構造化した要約に残り、集計はそこから行う。"""
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    n = 30
    Counting.listing = {date(2026, 6, 22): [_m(f"F{i:02d}", "11110") for i in range(n)]}
    Counting.pkgs = {f"F{i:02d}": b"broken" for i in range(n)}
    Counting.fail = {f"F{i:02d}" for i in range(0, n, 2)}  # 半分は取得のエラー、残りは読み取りのエラー
    r1 = ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 22), end=date(2026, 6, 22))
    assert len(r1.message) <= 1000
    with SessionLocal() as s:
        d = _summary(s, r1.id)
        assert d["errors_total"] == n and len(d["errors"]) == n
        assert d["stats"]["documents_failed_api"] == 15 and d["stats"]["documents_failed_parse"] == 15
    # 認証エラーの実行を2回（メッセージに頼らずに数える）
    class Denied(Counting):
        def documents(self, d):
            raise E.EdinetAuthError("EDINET の認証に失敗しました（401）")

    monkeypatch.setattr(E, "EdinetClient", Denied)
    for _ in range(2):
        ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 23), end=date(2026, 6, 23))
    from app.models import IngestionRun
    with SessionLocal() as s:  # メッセージを書き換えても（切れた場合を想定）集計は変わらない
        for run in s.query(IngestionRun).filter_by(dataset=ING.DATASET):
            run.message = "x"
        s.commit()
        rep = ING.report(s)
    assert rep["auth_error_runs"] == 2 and rep["runs_with_summary"] == 3
    assert rep["documents_failed_by_type"] == {"api": 15, "parse": 15, "store": 0, "unknown": 0}


@pytest.mark.parametrize("rate,ok", [(1.0, True), (0.10, True), (0.0001, True), (0, False), (-0.1, False), (1.0001, False),
                                     (float("nan"), False), ("abc", False)])
def test_mismatch_stop_rate_validation(client, monkeypatch, rate, ok):
    """m5：0 より大きく 1 以下だけを受け付ける。不正な値は、実行の記録も API への接続もする前に拒否する。"""
    from app.models import IngestionRun
    _setup_batch(monkeypatch)
    _jp_sec("1111")
    Counting.listing = {}
    if ok:
        assert ING.validate_stop_rate(rate) == float(rate)
        assert ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 22), end=date(2026, 6, 22),
                       mismatch_stop_rate=rate).status == "success"
    else:
        with pytest.raises(ValueError, match="0 より大きく 1 以下"):
            ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 22), end=date(2026, 6, 22), mismatch_stop_rate=rate)
        with SessionLocal() as s:
            assert s.query(IngestionRun).count() == 0 and Counting.lists == 0


@pytest.mark.parametrize("arg", ["0", "-0.5", "1.5", "nan"])
def test_worker_rejects_invalid_stop_rate_before_anything(monkeypatch, arg):
    import sys
    from app import worker
    called = []
    monkeypatch.setattr(worker, "init_db", lambda *a, **k: called.append("init_db"))
    monkeypatch.setattr(worker, "job", lambda *a, **k: called.append("job"))
    monkeypatch.setattr(sys, "argv", ["worker", "edinet", "--codes", "1111", "--start", "2026-06-22", "--end", "2026-06-22",
                                      "--mismatch-stop-rate", arg])
    with pytest.raises(SystemExit) as e:
        worker.main()
    assert e.value.code == 2 and called == []


def test_worker_passes_resume_flag(monkeypatch):
    import sys
    from app import worker
    seen = {}
    monkeypatch.setattr(worker, "init_db", lambda *a, **k: None)
    monkeypatch.setattr(worker, "job", lambda name, **kw: seen.update(kw))
    monkeypatch.setattr(sys, "argv", ["worker", "edinet", "--all-listed", "--start", "2026-06-22", "--end", "2026-06-22",
                                      "--mismatch-stop-rate", "1", "--resume-after-mismatch-stop"])
    worker.main()
    assert seen["resume_after_mismatch_stop"] is True and seen["mismatch_stop_rate"] == 1.0
