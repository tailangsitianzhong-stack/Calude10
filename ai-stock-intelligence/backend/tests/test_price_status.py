"""株価の接続状態の判定と表示のテスト（services/price_status.py）。日本株・米国株を分けて検証する。値はテスト用。

7つの状態：APIキー未設定／設定済み・未取得／取得成功・最新データあり／取得失敗・API障害／保存データあり・更新遅延／
一部銘柄のみ取得成功／取得元の切り替えによる代替取得成功。実際の外部APIには接続しない（取得の記録と株価をDBに直接置く）。
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app import config
from app.db import SessionLocal
from app.models import DailyPrice, FinancialFact, IngestionRun, Security
from app.services import ingest_us_prices, price_status

TODAY = date.today()
SECRET = "TEST-SECRET-VALUE-DO-NOT-SHOW"  # テスト用の仮のキー（応答に出ないことを確認する）


def _env(monkeypatch, **kv):
    for k, v in kv.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()


def _sec(market, code, typ="common", fin=False):
    with SessionLocal() as s:
        x = Security(market=market, code=code, name=f"{code} 株式会社", name_search=code.lower(), security_type=typ,
                     is_active=True, source="t", fetched_at=datetime.now(timezone.utc))
        s.add(x)
        s.flush()
        if fin:  # 米国株の取得対象（財務データのある銘柄）
            s.add(FinancialFact(security_id=x.id, concept="revenue", tag="Revenues", unit="USD", period_start=date(2025, 1, 1),
                                period_end=date(2025, 12, 31), form="10-K", accn=f"a{code}", filed=date(2026, 2, 1), value=1,
                                source="t", fetched_at=datetime.now(timezone.utc)))
        s.commit()
        return x.id


def _price(sid, d, provider, close=100.0, fetched=None):
    with SessionLocal() as s:
        s.add(DailyPrice(security_id=sid, trade_date=d, close=close, provider=provider,
                         fetched_at=fetched or datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)))
        s.commit()


def _run(dataset, status):
    with SessionLocal() as s:
        s.add(IngestionRun(dataset=dataset, source="t", status=status, rows=0, finished_at=datetime.now(timezone.utc)))
        s.commit()


def _quote(client, market, code):
    return client.get(f"/api/v1/securities/{market}/{code}").json()["quote"]


def _ds(client, key):
    return {d["dataset"]: d for d in client.get("/api/v1/system/status").json()["data_sources"]}[key]


JP_ON = dict(NETWORK_SECRETS="jquants", JQUANTS_PLAN="free")
US_ON = dict(NETWORK_SECRETS="tiingo,massive", US_PRICE_PROVIDER="tiingo")


# ---- 1. APIキー未設定 ----

def test_jp_not_configured(client, monkeypatch):
    _sec("JP", "7203")
    q = _quote(client, "JP", "7203")
    assert q["price_state"] == "not_configured" and q["price_state_label"] == "APIキー未設定"
    assert q["price"] is None and q["data_status"] == "not_connected" and "J-Quants" in q["note"]
    d = _ds(client, "prices_jp")
    assert d["price_state"] == "not_configured" and d["configured"] is False and d["status"] == "not_connected"


def test_us_not_configured(client, monkeypatch):
    _sec("US", "AAA", fin=True)
    q = _quote(client, "US", "AAA")
    assert q["price_state"] == "not_configured" and q["price"] is None and "Tiingo / Massive" in q["note"]
    assert _ds(client, "prices_us")["price_state"] == "not_configured"


def test_not_configured_takes_priority_over_old_failures_and_keeps_stored_price(client, monkeypatch):
    """複数条件：キーが未設定で、過去の取得失敗の記録と保存済みの株価がある → 「APIキー未設定」を優先。値は作らず、
    保存済みの株価は基準日つきで示す（更新されないことを明記）。"""
    sid = _sec("US", "AAA", fin=True)
    _price(sid, TODAY - timedelta(days=30), "tiingo", 50.0)
    _run("prices_us", "failed")
    q = _quote(client, "US", "AAA")
    assert q["price_state"] == "not_configured" and q["price"] == 50.0 and q["data_status"] == "stale"
    assert "更新していません" in q["note"] and str(TODAY - timedelta(days=30)) in q["note"]
    assert _ds(client, "prices_us")["price_state"] == "not_configured"


# ---- 2. APIキー設定済み・未取得 ----

@pytest.mark.parametrize("market,code,env", [("JP", "7203", JP_ON), ("US", "AAA", US_ON)])
def test_configured_not_fetched(client, monkeypatch, market, code, env):
    _env(monkeypatch, **env)
    _sec(market, code, fin=market == "US")
    q = _quote(client, market, code)
    assert q["price_state"] == "configured_not_fetched" and q["price_state_label"] == "設定済み・未取得"
    assert q["price"] is None and "未設定" not in q["note"] and "まだ取得していません" in q["note"]
    d = _ds(client, f"prices_{market.lower()}")
    assert d["price_state"] == "configured_not_fetched" and d["configured"] is True and d["fetched_count"] == 0
    px = client.get(f"/api/v1/securities/{market}/{code}/prices").json()
    assert px["price_state"] == "configured_not_fetched" and px["points"] == []
    config.get_settings.cache_clear()


def test_us_configured_when_only_fallback_has_key(client, monkeypatch):
    """主の取得元（Tiingo）にキーがなく、代替（Massive）にだけキーがある → 取得の処理と同じく「設定済み」。"""
    _env(monkeypatch, NETWORK_SECRETS="massive", US_PRICE_PROVIDER="tiingo")
    _sec("US", "AAA", fin=True)
    assert ingest_us_prices.provider_chain(config.get_settings()) == ["massive"]
    assert _quote(client, "US", "AAA")["price_state"] == "configured_not_fetched"
    assert _ds(client, "prices_us")["configured"] is True
    config.get_settings.cache_clear()


# ---- 3. 取得成功・最新データあり（基準日と取得日時を分ける） ----

def test_us_ok_separates_trade_date_and_fetched_at(client, monkeypatch):
    _env(monkeypatch, **US_ON)
    sid = _sec("US", "AAA", fin=True)
    d = TODAY - timedelta(days=1)
    _price(sid, d, "tiingo", 123.0, fetched=datetime(2026, 11, 2, 22, 5, tzinfo=timezone.utc))
    _run("prices_us", "success")
    q = _quote(client, "US", "AAA")
    assert q["price_state"] == "ok" and q["price_state_label"] == "取得済み" and q["price"] == 123.0
    assert q["as_of"] == d.isoformat() and q["fetched_at"].startswith("2026-11-02T22:05")  # 基準日と取得日時は別
    ds = _ds(client, "prices_us")
    assert ds["price_state"] == "ok" and ds["latest_data_date"] == d.isoformat() and ds["fetched_count"] == ds["target_count"] == 1
    config.get_settings.cache_clear()


def test_jp_ok_with_free_plan_delay_is_not_stale(client, monkeypatch):
    """Freeプラン（約12週間遅延）の株価は、遅延の範囲内なら「取得済み（Freeプラン・約12週間遅延）」。"""
    _env(monkeypatch, **JP_ON)
    sid = _sec("JP", "7203")
    _price(sid, TODAY - timedelta(days=85), "jquants", 2500.0)
    _run("prices_jp", "success")
    q = _quote(client, "JP", "7203")
    assert q["price_state"] == "ok" and q["price_state_label"] == "取得済み（Freeプラン・約12週間遅延）"
    assert q["data_status"] == "ok" and "リアルタイムの株価ではありません" in q["note"]
    d = _ds(client, "prices_jp")
    assert d["price_state"] == "ok" and d["delay"] == "Freeプラン・約12週間遅延"
    config.get_settings.cache_clear()


def test_jp_delay_comes_from_plan_spec_not_fixed(client, monkeypatch):
    """遅延は providers/jquants.py のプランの仕様から決める（12週間をこの判定に固定しない）。"""
    from app.providers import jquants
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN="light")
    sid = _sec("JP", "7203")
    _price(sid, TODAY - timedelta(days=85), "jquants")
    _run("prices_jp", "success")
    assert _quote(client, "JP", "7203")["price_state"] == "stale"  # Light は前営業日まで：85日前は古い
    monkeypatch.setitem(jquants.DELAY_DAYS, "light", 90)  # 仕様の表が変われば判定も変わる
    q = _quote(client, "JP", "7203")
    assert q["price_state"] == "ok" and q["price_state_label"] == "取得済み（Lightプラン・約90日遅延）"
    config.get_settings.cache_clear()


def test_jp_plan_unconfirmed_is_not_guessed(client, monkeypatch):
    """JQUANTS_PLAN が未設定：プランを推測しない。遅延の有無を判定できないことを明示する。"""
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN=None)
    assert config.get_settings().jquants_plan_specified is False
    sid = _sec("JP", "7203")
    _price(sid, TODAY - timedelta(days=85), "jquants")
    _run("prices_jp", "success")
    q = _quote(client, "JP", "7203")
    assert q["price_state"] == "ok" and q["price_state_label"] == "取得済み（プラン未確認・遅延を判定できません）"
    assert "Free" not in q["note"] and "JQUANTS_PLAN 未設定" in q["note"]
    d = _ds(client, "prices_jp")
    assert "プラン未確認" in d["delay"] and not any("Freeプラン" in w for w in d["warnings"])
    assert any("JQUANTS_PLAN" in w for w in d["warnings"])
    config.get_settings.cache_clear()


# ---- 4. 取得失敗・API障害 ----

@pytest.mark.parametrize("market,code,env", [("JP", "7203", JP_ON), ("US", "AAA", US_ON)])
def test_provider_error_without_and_with_stored_price(client, monkeypatch, market, code, env):
    _env(monkeypatch, **env)
    sid = _sec(market, code, fin=market == "US")
    ds_key = f"prices_{market.lower()}"
    _run(ds_key, "failed")
    q = _quote(client, market, code)
    assert q["price_state"] == "provider_error" and q["price"] is None and q["data_status"] == "error" and "障害" in q["note"]
    assert ("代わりの取得元はありません" in q["note"]) == (market == "JP")
    d0 = TODAY - timedelta(days=3)
    _price(sid, d0, "jquants" if market == "JP" else "tiingo", 77.0)
    q = _quote(client, market, code)
    assert q["price_state"] == "provider_error" and q["price"] == 77.0 and q["as_of"] == d0.isoformat()
    assert q["data_status"] == "stale" and str(d0) in q["note"]
    assert _ds(client, ds_key)["price_state"] == "provider_error" and _ds(client, ds_key)["status"] == "error"
    config.get_settings.cache_clear()


def test_skipped_and_interrupted_runs_are_not_outages(client, monkeypatch):
    _env(monkeypatch, **US_ON)
    _sec("US", "AAA", fin=True)
    _run("prices_us", "failed")
    _run("prices_us", "skipped")      # その時点では未設定だった
    _run("prices_us", "interrupted")  # 処理の中断
    assert _quote(client, "US", "AAA")["price_state"] == "provider_error"  # 最後に「終わった」実行は failed のまま
    _run("prices_us", "success")
    assert _quote(client, "US", "AAA")["price_state"] == "configured_not_fetched"
    config.get_settings.cache_clear()


# ---- 5. 保存データあり・更新遅延 ----

@pytest.mark.parametrize("market,code,env,age", [("JP", "7203", JP_ON, 84 + 8), ("US", "AAA", US_ON, 8)])
def test_stale_beyond_provider_delay(client, monkeypatch, market, code, env, age):
    _env(monkeypatch, **env)
    sid = _sec(market, code, fin=market == "US")
    _price(sid, TODAY - timedelta(days=age), "jquants" if market == "JP" else "tiingo")
    _run(f"prices_{market.lower()}", "success")
    q = _quote(client, market, code)
    assert q["price_state"] == "stale" and q["price_state_label"] == "古いデータ" and q["data_status"] == "stale"
    assert _ds(client, f"prices_{market.lower()}")["price_state"] == "stale"
    # 1日新しければ遅延の範囲内
    _price(sid, TODAY - timedelta(days=age - 1), "jquants" if market == "JP" else "tiingo")
    assert _quote(client, market, code)["price_state"] == "ok"
    config.get_settings.cache_clear()


def test_jp_plan_unconfirmed_old_beyond_any_plan_is_stale(client, monkeypatch):
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN=None)
    sid = _sec("JP", "7203")
    _price(sid, TODAY - timedelta(days=200), "jquants")
    _run("prices_jp", "success")
    assert _quote(client, "JP", "7203")["price_state"] == "stale"
    config.get_settings.cache_clear()


# ---- 6. 一部銘柄のみ取得成功（分母は取得対象の銘柄数） ----

def test_us_partial_uses_target_count_as_denominator(client, monkeypatch):
    _env(monkeypatch, **US_ON)
    a = _sec("US", "AAA", fin=True)
    _sec("US", "BBB", fin=True)
    _sec("US", "CCC", fin=True)
    z = _sec("US", "ZZZ", fin=False)  # 財務データなし：取得対象外（Massive の全銘柄の取得で株価があっても分母・分子に数えない）
    _price(a, TODAY - timedelta(days=1), "tiingo")
    _price(z, TODAY - timedelta(days=1), "massive")
    _run("prices_us", "success")
    d = _ds(client, "prices_us")
    assert d["price_state"] == "partial" and (d["fetched_count"], d["target_count"]) == (1, 3) and d["rows"] == 1
    assert d["price_state_label"] == "一部取得（1／3 銘柄）" and d["status"] == "ok"
    assert _quote(client, "US", "AAA")["price_state"] == "ok"  # 銘柄ごとには、その銘柄の状態
    q = _quote(client, "US", "BBB")
    assert q["price_state"] == "configured_not_fetched" and "取得対象 3 銘柄のうち 1 銘柄を取得済み" in q["note"]
    qz = _quote(client, "US", "ZZZ")
    assert qz["price_state"] == "fallback"  # ZZZ の株価は Massive（代替）の値
    config.get_settings.cache_clear()


def test_jp_partial_counts_active_jp_securities_and_last_run_partial(client, monkeypatch):
    _env(monkeypatch, **JP_ON)
    a = _sec("JP", "7203")
    _sec("JP", "1306", typ="etf")  # J-Quants の日足は ETF も含むので取得対象（分母）に入る
    _price(a, TODAY - timedelta(days=85), "jquants")
    _run("prices_jp", "success")
    d = _ds(client, "prices_jp")
    assert d["price_state"] == "partial" and (d["fetched_count"], d["target_count"]) == (1, 2)
    _price(_sec("JP", "9999"), TODAY - timedelta(days=85), "jquants")  # 全銘柄そろっても…
    with SessionLocal() as s:
        s.query(Security).filter_by(code="1306").update({"is_active": False})  # 上場廃止は分母から外れる
        s.commit()
    d = _ds(client, "prices_jp")
    assert (d["fetched_count"], d["target_count"]) == (2, 2) and d["price_state"] == "ok"
    _run("prices_jp", "partial")  # …最後の実行が partial なら「一部取得」
    assert _ds(client, "prices_jp")["price_state"] == "partial"
    config.get_settings.cache_clear()


# ---- 7. 取得元の切り替えによる代替取得（米国株） ----

def test_us_fallback_from_tiingo_to_massive_matches_provider_chain(client, monkeypatch):
    _env(monkeypatch, **US_ON)
    sid = _sec("US", "AAA", fin=True)
    assert ingest_us_prices.provider_chain(config.get_settings()) == ["tiingo", "massive"]
    _price(sid, TODAY - timedelta(days=1), "massive", 200.0)
    _run("prices_us", "success")
    q = _quote(client, "US", "AAA")
    assert q["price_state"] == "fallback" and q["price_state_label"] == "代替の取得元で取得（Massive（旧Polygon.io））"
    assert q["price"] == 200.0 and "主の取得元（Tiingo）" in q["note"] and q["data_status"] == "ok"
    d = _ds(client, "prices_us")
    assert d["price_state"] == "fallback" and d["used_provider"] == "Massive（旧Polygon.io）"
    # 同じ基準日に主の取得元の株価もあれば、主の取得元を使う（provider_chain の順）
    _price(sid, TODAY - timedelta(days=1), "tiingo", 201.0)
    q = _quote(client, "US", "AAA")
    assert q["price_state"] == "ok" and q["price"] == 201.0 and q["provider"] == "Tiingo"
    assert _ds(client, "prices_us")["price_state"] == "ok"
    # 主の取得元を massive にすると、Massive の株価は代替ではない（provider_chain と一致）
    _env(monkeypatch, US_PRICE_PROVIDER="massive")
    _price(sid, TODAY, "massive", 202.0)
    assert ingest_us_prices.provider_chain(config.get_settings())[0] == "massive"
    assert _quote(client, "US", "AAA")["price_state"] == "ok"
    config.get_settings.cache_clear()


def test_jp_never_reports_fallback(client, monkeypatch):
    _env(monkeypatch, **JP_ON)
    sid = _sec("JP", "7203")
    _price(sid, TODAY - timedelta(days=85), "jquants")
    _run("prices_jp", "success")
    assert _quote(client, "JP", "7203")["price_state"] == "ok" and _ds(client, "prices_jp")["price_state"] == "ok"
    config.get_settings.cache_clear()


# ---- 検索結果・EDINET の表示・キーを表示しないこと ----

def test_search_results_show_actual_price_state(client, monkeypatch):
    _env(monkeypatch, **US_ON)
    a = _sec("US", "AAA", fin=True)
    _sec("US", "AAB", fin=True)
    _sec("JP", "7203")
    _price(a, TODAY - timedelta(days=1), "tiingo")
    _run("prices_us", "success")
    items = {i["code"]: i for i in client.get("/api/v1/securities", params={"query": "AA"}).json()["items"]}
    assert items["AAA"]["price_state"] == "ok" and items["AAA"]["price_data_status"] == "ok"
    assert items["AAB"]["price_state"] == "configured_not_fetched" and items["AAB"]["price_state_label"] == "設定済み・未取得"
    jp = client.get("/api/v1/securities", params={"query": "7203"}).json()["items"][0]
    assert jp["price_state"] == "not_configured"  # 日本株のキーは未設定
    config.get_settings.cache_clear()


def test_edinet_provider_is_not_shown_as_unimplemented(client, monkeypatch):
    _env(monkeypatch, EDINET_API_KEY=SECRET)
    p = {x["key"]: x for x in client.get("/api/v1/system/status").json()["providers"]}["edinet"]
    assert p["status"] == "ok" and "未実装" not in p["message"] and p["configured"] is True
    _env(monkeypatch, EDINET_API_KEY="")
    p = {x["key"]: x for x in client.get("/api/v1/system/status").json()["providers"]}["edinet"]
    assert p["status"] == "not_connected" and "未実装" not in p["message"] and "EDINET_API_KEY" in p["missing_settings"]
    config.get_settings.cache_clear()


def test_key_values_never_appear_in_responses(client, monkeypatch, caplog):
    _env(monkeypatch, JQUANTS_API_KEY=SECRET, TIINGO_API_KEY=SECRET, MASSIVE_API_KEY=SECRET, US_PRICE_PROVIDER="tiingo",
         EDINET_API_KEY=SECRET, JQUANTS_PLAN="free")
    sid = _sec("US", "AAA", fin=True)
    _sec("JP", "7203")
    _price(sid, TODAY - timedelta(days=1), "massive")
    _run("prices_us", "failed")
    bodies = [client.get(u).text for u in ("/api/v1/securities/US/AAA", "/api/v1/securities/JP/7203",
                                           "/api/v1/securities/US/AAA/prices", "/api/v1/securities?query=AAA",
                                           "/api/v1/system/status")]
    assert all(SECRET not in b for b in bodies) and SECRET not in caplog.text
    assert _quote(client, "US", "AAA")["price_state"] == "provider_error"  # キーは設定済み（値は出さない）
    config.get_settings.cache_clear()


# ---- PR #10 の追加確認：市場全体の取得率と鮮度を両方示す／取得対象（分母）を取得の処理と一致させる ----

def _jp_market(n_targets, fetched_ages, plan="free"):
    """日本株の取得対象 n_targets 銘柄のうち、fetched_ages の数だけ株価を置く（値は基準日までの日数）。"""
    ids = [_sec("JP", f"{1000 + i}") for i in range(n_targets)]
    for sid, age in zip(ids, fetched_ages):
        _price(sid, TODAY - timedelta(days=age), "jquants")
    _run("prices_jp", "success")
    return ids


def test_partial_and_stale_are_both_shown(client, monkeypatch):
    """一部取得かつ古いデータ：市場全体では「一部取得（m／n 銘柄）・データ遅延あり」。銘柄ページはその銘柄自身の状態。"""
    _env(monkeypatch, **JP_ON)
    _jp_market(4, [84 + 30])  # 4銘柄のうち1銘柄だけ、Freeプランの遅延（84日）＋許容を超えて古い
    d = _ds(client, "prices_jp")
    assert d["price_state"] == "partial" and d["price_state_label"] == "一部取得（1／4 銘柄）・データ遅延あり"
    assert d["price_partial"] is True and d["price_freshness"] == "stale" and d["status"] == "stale"
    assert (d["fetched_count"], d["target_count"]) == (1, 4) and d["target_label"] == "上場中の日本株"
    assert _quote(client, "JP", "1000")["price_state"] == "stale"                 # 取得済みの銘柄：その銘柄は古いデータ
    assert _quote(client, "JP", "1001")["price_state"] == "configured_not_fetched"  # 未取得の銘柄
    config.get_settings.cache_clear()


def test_partial_and_fresh(client, monkeypatch):
    _env(monkeypatch, **JP_ON)
    _jp_market(4, [85, 85])
    d = _ds(client, "prices_jp")
    assert d["price_state"] == "partial" and d["price_state_label"] == "一部取得（2／4 銘柄）"
    assert d["price_partial"] is True and d["price_freshness"] == "fresh" and d["status"] == "ok"
    assert _quote(client, "JP", "1000")["price_state"] == "ok"
    config.get_settings.cache_clear()


def test_all_fetched_but_stale(client, monkeypatch):
    _env(monkeypatch, **JP_ON)
    _jp_market(3, [84 + 30] * 3)
    d = _ds(client, "prices_jp")
    assert d["price_state"] == "stale" and d["price_state_label"] == "古いデータ（全 3 銘柄取得済み）"
    assert d["price_partial"] is False and d["price_freshness"] == "stale" and d["status"] == "stale"
    config.get_settings.cache_clear()


def test_not_configured_with_past_prices_market_and_security(client, monkeypatch):
    """APIキー未設定かつ過去の株価あり：市場全体も銘柄も「APIキー未設定」を優先。取得率・鮮度・保存済みの株価は示す。"""
    a = _sec("US", "AAA", fin=True)
    _sec("US", "BBB", fin=True)
    _price(a, TODAY - timedelta(days=40), "tiingo", 55.0)
    _run("prices_us", "success")
    d = _ds(client, "prices_us")
    assert d["price_state"] == "not_configured" and d["price_state_label"] == "APIキー未設定" and d["configured"] is False
    assert (d["fetched_count"], d["target_count"]) == (1, 2) and d["price_freshness"] == "stale" and d["price_partial"] is True
    assert d["latest_data_date"] == (TODAY - timedelta(days=40)).isoformat()
    q = _quote(client, "US", "AAA")
    assert q["price_state"] == "not_configured" and q["price"] == 55.0 and "更新していません" in q["note"]


def test_provider_error_with_stored_prices_market_and_security(client, monkeypatch):
    """API障害かつ保存済みの株価あり：市場全体も銘柄も「取得失敗（API障害）」。保存済みの株価は基準日つきで示し、値は作らない。"""
    _env(monkeypatch, **JP_ON)
    ids = _jp_market(2, [85])
    _run("prices_jp", "failed")
    d = _ds(client, "prices_jp")
    assert d["price_state"] == "provider_error" and d["status"] == "error"
    assert (d["fetched_count"], d["target_count"]) == (1, 2) and d["price_freshness"] == "fresh"
    assert any("代わりの取得元はありません" in w for w in d["warnings"])
    q = _quote(client, "JP", "1000")
    assert q["price_state"] == "provider_error" and q["price"] == 100.0 and q["as_of"] == (TODAY - timedelta(days=85)).isoformat()
    assert _quote(client, "JP", "1001")["price_state"] == "provider_error" and _quote(client, "JP", "1001")["price"] is None
    config.get_settings.cache_clear()


def test_us_target_matches_tiingo_fetch_selection(client, monkeypatch):
    """米国株・主の取得元が Tiingo：分母は ingest_us_prices._tiingo() が実際に取得しに行く銘柄と一致する
    （偽のクライアントで、要求した銘柄を記録する。外部APIには接続しない）。SEC の対象を増やせば分母も自動的に増える。"""
    from app.providers import us_prices
    _env(monkeypatch, **US_ON)
    for c in ("AAA", "BBB", "CCC"):
        _sec("US", c, fin=True)
    _sec("US", "NOF", fin=False)   # 財務データなし：Tiingo は取得しない
    asked = []

    class Recording:
        def __init__(self, *a, **k):
            pass

        def daily(self, ticker, start):
            asked.append(ticker)
            return []

        def close(self):
            pass

    monkeypatch.setattr(us_prices, "TiingoClient", Recording)
    with SessionLocal() as s:
        ingest_us_prices._tiingo(s, config.get_settings())
        ctx = price_status.market_context(s, "US")
        assert ctx.target_basis == "tiingo"
        target_codes = {c for (c,) in s.execute(select_codes(price_status.target_ids(ctx)))}
    assert set(asked) == target_codes == {"AAA", "BBB", "CCC"}
    assert _ds(client, "prices_us")["target_count"] == 3
    _sec("US", "DDD", fin=True)  # SEC の対象が増えた
    assert _ds(client, "prices_us")["target_count"] == 4
    config.get_settings.cache_clear()


def test_us_target_follows_primary_provider_massive(client, monkeypatch):
    """米国株・主の取得元が Massive：grouped daily は市場全体を返すので、分母は上場中の米国株すべて。"""
    _env(monkeypatch, NETWORK_SECRETS="massive", US_PRICE_PROVIDER="massive")
    _sec("US", "AAA", fin=True)
    nof = _sec("US", "NOF", fin=False)
    with SessionLocal() as s:
        s.add(Security(market="US", code="OLD", name="old", name_search="old", security_type="common", is_active=False,
                       source="t", fetched_at=datetime.now(timezone.utc)))
        s.commit()
    _price(nof, TODAY - timedelta(days=1), "massive")
    _run("prices_us", "success")
    d = _ds(client, "prices_us")
    assert d["target_label"] == "上場中の米国株" and (d["fetched_count"], d["target_count"]) == (1, 2)  # 上場廃止は数えない
    assert d["price_state_label"] == "一部取得（1／2 銘柄）"
    q = _quote(client, "US", "AAA")
    assert q["price_state"] == "configured_not_fetched" and "含まれていません" not in q["note"]  # Massive の取得対象に入る
    assert "取得対象 2 銘柄のうち 1 銘柄を取得済み" in q["note"]
    config.get_settings.cache_clear()


def select_codes(ids_query):
    from sqlalchemy import select
    return select(Security.code).where(Security.id.in_(ids_query))
