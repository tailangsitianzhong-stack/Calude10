"""株価の取得元の障害時の切り替えのテスト（日本株と米国株を分けて検証する）。値はテスト用。

- 米国株：主の取得元（Tiingo / Massive）が使えなければ、もう一方に切り替える。どちらも使えなければ失敗として記録し、
  代わりの値は作らない。画面は保存済みの終値を基準日つきで示し、障害であることを明示する。
- 日本株：J-Quants 以外に使える取得元がない。障害時は値を作らず、障害であることを明示する。
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from app import config
from app.db import SessionLocal
from app.models import DailyPrice, DataQualityCheck, FinancialFact, IngestionRun, Security
from app.providers import jquants, us_prices
from app.services import ingest_jquants, ingest_us_prices

TODAY = date.today()


def _env(monkeypatch, **kv):
    for k, v in kv.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()


@pytest.fixture()
def us_co(client):
    with SessionLocal() as s:
        x = Security(market="US", code="AAA", cik=1, name="AAA Inc", name_search="aaa", security_type="common",
                     is_active=True, source="t", fetched_at=datetime.now(timezone.utc))
        s.add(x)
        s.flush()
        s.add(FinancialFact(security_id=x.id, concept="revenue", tag="Revenues", unit="USD", period_start=date(2025, 1, 1),
                            period_end=date(2025, 12, 31), form="10-K", accn="a", filed=date(2026, 2, 1), value=1,
                            source="t", fetched_at=datetime.now(timezone.utc)))
        s.commit()
        return x.id


class TiingoDown:
    def __init__(self, *a, **k):
        pass

    def daily(self, ticker, start):
        raise PermissionError("Tiingo の認証に失敗しました（401）")

    def close(self):
        pass


class TiingoNetworkDown(TiingoDown):
    def daily(self, ticker, start):
        raise RuntimeError("通信エラー：ConnectError https://api.tiingo.com/...")


class MassiveOK:
    def __init__(self, *a, **k):
        pass

    def grouped_daily(self, d):
        return [{"code": "AAA", "date": d, "close": 100.0 + d.day}]

    def close(self):
        pass


class MassiveDown(MassiveOK):
    def grouped_daily(self, d):
        raise PermissionError("Massive の認証・プランの範囲外です（403）")


def test_provider_chain(client, monkeypatch):
    _env(monkeypatch, US_PRICE_PROVIDER="tiingo", NETWORK_SECRETS="tiingo,massive")
    assert ingest_us_prices.provider_chain(config.get_settings()) == ["tiingo", "massive"]
    _env(monkeypatch, US_PRICE_PROVIDER="polygon", US_PRICE_FALLBACK="tiingo")
    assert ingest_us_prices.provider_chain(config.get_settings()) == ["massive", "tiingo"]
    _env(monkeypatch, US_PRICE_PROVIDER="tiingo", US_PRICE_FALLBACK="", NETWORK_SECRETS="tiingo")
    assert ingest_us_prices.provider_chain(config.get_settings()) == ["tiingo"]  # キーのない代替は使わない
    config.get_settings.cache_clear()


@pytest.mark.parametrize("down", [TiingoDown, TiingoNetworkDown])
def test_us_switches_to_fallback_when_primary_is_down(us_co, monkeypatch, down):
    _env(monkeypatch, US_PRICE_PROVIDER="tiingo", NETWORK_SECRETS="tiingo,massive")
    monkeypatch.setattr(us_prices, "TiingoClient", down)
    monkeypatch.setattr(us_prices, "MassiveClient", MassiveOK)
    with SessionLocal() as s:
        r = ingest_us_prices.run(s)
        assert r.status == "success" and r.rows > 0
        assert "切り替え" in r.source and "Tiingo の障害のため Massive" in r.message
        assert {p.provider for p in s.query(DailyPrice).all()} == {"massive"}
    config.get_settings.cache_clear()


def test_us_without_fallback_records_failure_and_keeps_stored_price(us_co, client, monkeypatch):
    _env(monkeypatch, US_PRICE_PROVIDER="tiingo", NETWORK_SECRETS="tiingo")
    monkeypatch.setattr(us_prices, "TiingoClient", TiingoDown)
    d = TODAY - timedelta(days=3)
    with SessionLocal() as s:
        s.add(DailyPrice(security_id=us_co, trade_date=d, close=50.0, provider="tiingo", fetched_at=datetime.now(timezone.utc)))
        s.commit()
        r = ingest_us_prices.run(s)
        assert r.status == "failed" and r.rows == 0 and "代替の取得元が未設定" in r.message
        assert s.query(DailyPrice).count() == 1  # 値を作らない・消さない
    q = client.get("/api/v1/securities/US/AAA").json()["quote"]
    assert q["price"] == 50.0 and q["as_of"] == d.isoformat()  # 保存済みの終値を基準日つきで
    assert q["data_status"] == "stale" and "障害" in q["note"] and str(d) in q["note"]
    ds = {x["dataset"]: x for x in client.get("/api/v1/system/status").json()["data_sources"]}["prices_us"]
    assert ds["status"] == "error" and any("障害" in w for w in ds["warnings"])
    config.get_settings.cache_clear()


def test_us_all_providers_down(us_co, client, monkeypatch):
    _env(monkeypatch, US_PRICE_PROVIDER="tiingo", NETWORK_SECRETS="tiingo,massive")
    monkeypatch.setattr(us_prices, "TiingoClient", TiingoDown)
    monkeypatch.setattr(us_prices, "MassiveClient", MassiveDown)
    with SessionLocal() as s:
        r = ingest_us_prices.run(s)
        assert r.status == "failed" and "Tiingo" in r.message and "Massive" in r.message
    q = client.get("/api/v1/securities/US/AAA").json()["quote"]
    assert q["price"] is None and q["data_status"] == "error" and "障害" in q["note"]  # 値は作らない
    config.get_settings.cache_clear()


def test_us_cross_provider_close_mismatch_is_recorded(us_co, monkeypatch):
    with SessionLocal() as s:
        d = TODAY - timedelta(days=2)
        s.add(DailyPrice(security_id=us_co, trade_date=d, close=100.0, provider="tiingo", fetched_at=datetime.now(timezone.utc)))
        s.add(DailyPrice(security_id=us_co, trade_date=d, close=103.0, provider="massive", fetched_at=datetime.now(timezone.utc)))
        s.commit()
        assert ingest_us_prices.cross_check(s) == 1
        s.commit()
        assert s.query(DataQualityCheck).filter_by(dataset="prices_us", check_type="inconsistent").count() == 1


def test_jp_outage_has_no_substitute_and_is_explicit(client, monkeypatch):
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN="free")

    class Down:
        def __init__(self, *a, **k):
            pass

        def topix(self, start=None):
            raise jquants.PlanNotAvailable("TOPIX はプラン外")

        def daily_bars_by_date(self, d):
            raise PermissionError("J-Quants の認証に失敗しました（401）")

        def close(self):
            pass

    monkeypatch.setattr(jquants, "JQuantsClient", Down)
    with SessionLocal() as s:
        s.add(Security(market="JP", code="7203", name="トヨタ自動車", name_search="とよた", security_type="common",
                       is_active=True, source="t", fetched_at=datetime.now(timezone.utc)))
        s.commit()
        r = ingest_jquants.run(s)
        assert r.status == "failed" and "代わりの取得元はない" in r.message
        assert s.query(DailyPrice).count() == 0
    q = client.get("/api/v1/securities/JP/7203").json()["quote"]
    assert q["price"] is None and q["data_status"] == "error" and "J-Quants" in q["note"] and "代わりの取得元はありません" in q["note"]
    ds = {x["dataset"]: x for x in client.get("/api/v1/system/status").json()["data_sources"]}["prices_jp"]
    assert ds["status"] == "error" and any("代わりの取得元はありません" in w for w in ds["warnings"])
    config.get_settings.cache_clear()


def test_massive_max_days_limits_requests(us_co, monkeypatch):
    """無料プラン（5リクエスト/分）のため、Massive の1回の取得日数を MASSIVE_MAX_DAYS で制限できる。"""
    _env(monkeypatch, US_PRICE_PROVIDER="massive", NETWORK_SECRETS="massive", MASSIVE_MAX_DAYS="2")
    seen = []

    class Counting(MassiveOK):
        def grouped_daily(self, d):
            seen.append(d)
            return super().grouped_daily(d)

    monkeypatch.setattr(us_prices, "MassiveClient", Counting)
    with SessionLocal() as s:
        assert ingest_us_prices.run(s).status == "success"
    assert len(seen) == 2
    config.get_settings.cache_clear()
