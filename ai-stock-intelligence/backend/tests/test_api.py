from app.db import SessionLocal
from app.services import security_master as sm


def load(raw):
    with SessionLocal() as s:
        rows, as_of = sm.parse_jpx(raw / "data_j.xlsx")
        sm.upsert(s, rows, sm.JPX_SOURCE, as_of, "securities_jp")
        rows, as_of = sm.parse_nasdaq(raw / "nasdaqlisted.txt", raw / "otherlisted.txt")
        sm.upsert(s, rows, sm.NASDAQ_SOURCE, as_of, "securities_us")


def test_parse_types_and_dates(raw_files):
    rows, as_of = sm.parse_jpx(raw_files / "data_j.xlsx")
    assert str(as_of) == "2026-09-30"
    types = {r["code"]: r["security_type"] for r in rows}
    assert types == {"7203": "common", "8306": "common", "1306": "etf", "130A": "common"}
    rows, as_of = sm.parse_nasdaq(raw_files / "nasdaqlisted.txt", raw_files / "otherlisted.txt")
    assert str(as_of) == "2026-11-03"
    types = {r["code"]: r["security_type"] for r in rows}
    assert "ZZZT" not in types  # テスト銘柄は除外
    assert types["QQQ"] == "etf" and types["JPM$C"] == "other" and types["KO"] == "common"


def test_search_by_code_name_and_fullwidth(client, raw_files):
    load(raw_files)
    r = client.get("/api/v1/securities", params={"query": "7203"}).json()
    assert r["items"][0]["name"] == "トヨタ自動車"
    # 全角の銘柄名を半角・大文字小文字の違いを吸収して検索できる
    r = client.get("/api/v1/securities", params={"query": "三菱ufj"}).json()
    assert r["items"][0]["code"] == "8306"
    r = client.get("/api/v1/securities", params={"query": "ko", "market": "US"}).json()
    assert r["items"][0]["code"] == "KO"  # コードの完全一致が先頭
    # ファンドは既定で除外、指定すれば含める
    assert client.get("/api/v1/securities", params={"query": "1306"}).json()["total"] == 0
    assert client.get("/api/v1/securities", params={"query": "1306", "include_funds": True}).json()["total"] == 1


def test_no_fabricated_market_data(client, raw_files):
    load(raw_files)
    for idx in client.get("/api/v1/market/overview").json():
        assert idx["value"] is None and idx["data_status"] == "not_connected"
    d = client.get("/api/v1/securities/US/KO").json()
    assert d["quote"]["price"] is None and d["quote"]["data_status"] == "not_connected"
    for sec in client.get("/api/v1/securities/JP/7203/sections").json():
        assert sec["data_status"] == "not_implemented"
    assert client.get("/api/v1/securities/US/NOPE").status_code == 404


def test_system_status_reports_unconnected_providers_and_datasets(client, raw_files):
    load(raw_files)
    st = client.get("/api/v1/system/status").json()
    prov = {p["key"]: p for p in st["providers"]}
    assert prov["jquants"]["status"] == "not_connected" and prov["us_prices"]["status"] == "not_connected"
    assert prov["edinet"]["status"] == "not_connected"
    ds = {d["dataset"]: d for d in st["datasets"]}
    assert ds["securities_jp"]["rows"] == 4 and ds["securities_jp"]["source_as_of"] == "2026-09-30"
    # 秘密情報の値は返さない（設定名だけを返す）
    assert "JQUANTS_API_KEY" in str(st) and "change-me" not in str(st)


def test_delisted_kept_inactive(client, raw_files):
    load(raw_files)
    with SessionLocal() as s:
        rows, as_of = sm.parse_jpx(raw_files / "data_j.xlsx")
        sm.upsert(s, [r for r in rows if r["code"] != "7203"], sm.JPX_SOURCE, as_of, "securities_jp")
    assert client.get("/api/v1/securities", params={"query": "7203"}).json()["total"] == 0
    assert client.get("/api/v1/securities/JP/7203").status_code == 200  # 削除せず残す（生存者バイアス対策）


def test_search_orders_large_caps_first(client, raw_files):
    load(raw_files)
    with SessionLocal() as s:
        from app.models import Security
        from app.services.text import normalize
        from datetime import datetime, timezone
        s.add(Security(market="JP", code="3116", name="トヨタ紡織", name_search=normalize("トヨタ紡織 3116"), exchange="プライム（内国株式）",
                       sector="輸送用機器", size_class="TOPIX Mid400", security_type="common", is_active=True,
                       source="test", fetched_at=datetime.now(timezone.utc)))
        s.commit()
    r = client.get("/api/v1/securities", params={"query": "トヨタ"}).json()
    assert [i["code"] for i in r["items"]][:2] == ["7203", "3116"]


def test_market_overview_uses_stored_index_and_marks_topix_unconnected(client):
    from datetime import date, datetime, timezone
    from app.models import IndexBar
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add_all([IndexBar(index_key="sp500", trade_date=date.today(), close=110.0, provider="FRED", fetched_at=now),
                   IndexBar(index_key="sp500", trade_date=date(2020, 1, 2), close=100.0, provider="FRED", fetched_at=now)])
        s.commit()
    ov = {q["key"]: q for q in client.get("/api/v1/market/overview").json()}
    assert ov["sp500"]["value"] == 110.0 and ov["sp500"]["is_delayed"] is True and "リアルタイムではありません" in ov["sp500"]["note"]
    assert ov["topix"]["value"] is None and ov["topix"]["data_status"] == "not_connected"


def test_status_reports_missing_sec_contact_and_unconnected_price_apis(client):
    st = client.get("/api/v1/system/status").json()
    ds = {d["dataset"]: d for d in st["data_sources"]}
    assert ds["prices_jp"]["status"] == "not_connected" and ds["prices_us"]["status"] == "not_connected"
    assert any("SEC_USER_AGENT" in w for w in ds["sec_financials"]["warnings"])
    sec_p = {p["key"]: p for p in st["providers"]}["sec_edgar"] if "providers" in st else None
    if sec_p:
        assert sec_p["configured"] is False and sec_p["status"] == "not_connected"
