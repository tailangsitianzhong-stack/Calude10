"""プロバイダー障害時の切り替えのテスト（設計書 14章 フェーズ3の完了条件、13章「障害時」）。

設計書の約束：取得に失敗したプロバイダーは別の取得元に切り替え、すべて失敗したら保存済みのデータで画面を表示し、
「古いデータ」を明示する。値はテスト用。
"""
from datetime import date, datetime, timezone

import httpx

from app.db import SessionLocal
from app.models import FinancialFact, IngestionRun, Security
from app.providers import fred, jquants
from app.services import ingest_fred, ingest_jquants_fins
from tests.test_jp_financials import rec


def _jp(code):
    with SessionLocal() as s:
        x = Security(market="JP", code=code, name=code, name_search=code, security_type="common", is_active=True,
                     source="test", fetched_at=datetime.now(timezone.utc))
        s.add(x)
        s.commit()
        return x.id


def _fact(sid, concept, tag, end, value, form, accn, filed, start=None):
    return FinancialFact(security_id=sid, concept=concept, tag=tag, unit="JPY", period_start=start, period_end=end,
                         fiscal_year=end.year, fiscal_period="FY", form=form, accn=accn, filed=filed, value=value,
                         source="test", fetched_at=datetime.now(timezone.utc))


def _edinet_doc(doc_id, sid, standard, period_end=date(2026, 3, 31), submitted="2026-06-20 15:00"):
    from app.models import EdinetDocument
    return EdinetDocument(doc_id=doc_id, security_id=sid, sec_code="00000", edinet_code="E0", filer_name="t", form_code="030000",
                          doc_description="有価証券報告書", submitted_at=submitted, period_end=period_end,
                          accounting_standard=standard, parse_status="parsed", detail={}, fetched_at=datetime.now(timezone.utc))


def test_fred_switches_from_api_to_public_csv_when_key_rejected(client, monkeypatch):
    """FRED：APIキーが拒否されたら、公開CSVに切り替えて取得を続ける。"""
    calls = []

    def fake_get(self, url, **kw):
        calls.append(url)
        req = httpx.Request("GET", url)
        if url == fred.API:
            return httpx.Response(400, text='{"error_message": "Bad Request. The value for variable api_key is not registered."}', request=req)
        return httpx.Response(200, text="observation_date,X\n2026-10-01,1.5\n2026-10-02,1.6\n", request=req)

    monkeypatch.setattr(fred.RateLimitedClient, "get", fake_get)
    monkeypatch.setenv("FRED_API_KEY", "invalid")
    from app import config
    config.get_settings.cache_clear()
    with SessionLocal() as s:
        r = ingest_fred.run(s, keys=[fred.SERIES[0].key])
    assert r.status == "success" and r.rows == 2 and "切り替え" in r.source
    assert calls[0] == fred.API and calls[-1] == fred.BASE  # API → CSV の順
    config.get_settings.cache_clear()


def test_jquants_outage_keeps_stored_data_and_records_failure(client, monkeypatch):
    """J-Quants が使えないとき：取得は失敗として記録し、保存済みの財務データはそのまま表示する（消さない）。"""
    sid = _jp("4444")
    with SessionLocal() as s:
        ingest_jquants_fins.store_records(s, [rec("K1", "44440", "2026-05-10", "FYFinancialStatements_Consolidated_JP", "FY",
                                                  "2025-04-01", "2026-03-31", "2025-04-01", "2026-03-31", Sales=1000, TA=5000, Eq=2000)],
                                          {"4444": sid}, datetime.now(timezone.utc))
        s.commit()

    class Down:
        def __init__(self, *a, **k):
            pass

        def fins_summary(self, code=None, disc_date=None):
            raise RuntimeError("通信エラー：ConnectError https://api.jquants.com/v2/fins/summary")

        def close(self):
            pass

    monkeypatch.setenv("NETWORK_SECRETS", "jquants")
    from app import config
    config.get_settings.cache_clear()
    monkeypatch.setattr(jquants, "JQuantsClient", Down)
    with SessionLocal() as s:
        r = ingest_jquants_fins.run(s, codes=["4444"])
        assert r.status == "failed" and "通信エラー" in r.message
        assert s.query(FinancialFact).filter_by(security_id=sid).count() == 3  # 保存済みの値は残る
    j = client.get("/api/v1/securities/JP/4444/financials").json()
    assert j["annual"][-1]["values"]["revenue"] == 1000  # 保存済みのデータで表示を続ける
    ds = {d["dataset"]: d for d in client.get("/api/v1/system/status").json()["data_sources"]}["financials_jp"]
    assert ds["last_run_status"] == "failed" and ds["rows"] == 1 and "通信エラー" in ds["message"]
    config.get_settings.cache_clear()


def test_balance_sheet_falls_back_to_edinet_when_jquants_has_no_value(client):
    """J-Quants（決算短信）に値がない期（Freeプランの範囲外・取得失敗）は、EDINET の報告値に切り替えて表示する。"""
    sid = _jp("3333")
    with SessionLocal() as s:
        # 決算短信（日本基準）：損益だけ（貸借対照表の値を取得できなかった想定）。実際と同じく開示の記録とともに保存する
        ingest_jquants_fins.store_records(s, [rec("T1", "33330", "2026-05-10", "FYFinancialStatements_Consolidated_JP", "FY",
                                                  "2025-04-01", "2026-03-31", "2025-04-01", "2026-03-31", Sales=1000)],
                                          {"3333": sid}, datetime.now(timezone.utc))
        s.add(_edinet_doc("S1", sid, "JGAAP"))
        s.add_all([
            # EDINET の有価証券報告書（日本基準）：総資産・負債・純資産
            _fact(sid, "assets", "edinet:jppfs_cor:Assets", date(2026, 3, 31), 5000, "EDINET-ASR", "S1", date(2026, 6, 20)),
            _fact(sid, "liabilities", "edinet:jppfs_cor:Liabilities", date(2026, 3, 31), 3000, "EDINET-ASR", "S1", date(2026, 6, 20)),
            _fact(sid, "net_assets", "edinet:jppfs_cor:NetAssets", date(2026, 3, 31), 2000, "EDINET-ASR", "S1", date(2026, 6, 20)),
        ])
        s.commit()
    j = client.get("/api/v1/securities/JP/3333/financials").json()
    assert j["balance"]["values"] == {"assets": 5000, "liabilities": 3000, "net_assets": 2000}
    assert j["balance"]["tags"]["assets"].startswith("edinet:")
    assert j["annual"][-1]["values"]["revenue"] == 1000 and j["annual"][-1]["values"]["liabilities"] == 3000
    # 時点を守る：EDINET の提出（6/20）より前の時点では、まだ知り得ない
    old = client.get("/api/v1/securities/JP/3333/financials", params={"as_of": "2026-06-01"}).json()
    assert old["balance"] is None


def test_all_providers_down_shows_stored_data_as_stale(client):
    """すべての取得元が止まり更新されないまま時間がたったら、保存済みの値を「古いデータ」として表示する。"""
    sid = _jp("2222")
    with SessionLocal() as s:
        ingest_jquants_fins.store_records(s, [rec("T2", "22220", "2026-05-10", "FYFinancialStatements_Consolidated_JP", "FY",
                                                  "2025-04-01", "2026-03-31", "2025-04-01", "2026-03-31", Sales=1000, TA=5000)],
                                          {"2222": sid}, datetime.now(timezone.utc))
        s.add(IngestionRun(dataset="financials_jp", source="test", status="failed", rows=0, message="J-Quants 停止"))
        s.commit()
    fresh = client.get("/api/v1/securities/JP/2222/financials", params={"as_of": "2026-06-01"}).json()
    later = client.get("/api/v1/securities/JP/2222/financials", params={"as_of": "2027-06-01"}).json()
    assert fresh["data_status"] == "ok" and later["data_status"] == "stale"
    assert later["annual"][-1]["values"]["revenue"] == 1000  # 値は消さずに表示し、古いことを明示する
