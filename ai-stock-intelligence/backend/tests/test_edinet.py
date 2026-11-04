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
    with SessionLocal() as s:
        counts = (s.query(EdinetDocument).count(), s.query(FinancialFact).count(), s.query(DataQualityCheck).count())
        assert counts[0] == 1
    ING.run(SessionLocal(), codes=["1111"], start=date(2026, 6, 22), end=date(2026, 6, 22))  # 同じ日を銘柄指定でも
    with SessionLocal() as s:
        assert (s.query(EdinetDocument).count(), s.query(FinancialFact).count(), s.query(DataQualityCheck).count()) == counts
