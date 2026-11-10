"""J-Quants のプランの判定と表示のテスト（services/jquants_plan.py、config.Settings）。値はテスト用。

- 確認済み（free / light / standard / premium）：そのプランの仕様で遅延・利用条件を示す
- 未設定・空：「プラン未確認」。Free と表示しない。取得は Free 相当の安全側の条件のまま（従来どおり）
- 不正な値：「設定不備」。Free に置き換えない。アプリは起動し、J-Quants の取得だけを止める
- TOPIX：未取得・API がプランの範囲外と応答・プランの仕様で範囲外・プラン未確認・取得失敗を区別する
外部APIには接続しない（偽のクライアントを使う）。
"""
import logging
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

from app import config
from app.db import SessionLocal
from app.models import DataQualityCheck, IndexBar, IngestionRun, Security
from app.providers import jquants
from app.services import ingest_jquants, ingest_jquants_fins, jquants_plan
from app.services.ingest_jquants_fins import store_records, window_dates
from tests.test_jp_financials import rec

TODAY = date.today()
PLANS = ("free", "light", "standard", "premium")


def _env(monkeypatch, **kv):
    for k, v in kv.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()


def _st():
    return config.get_settings()


# ---- プランの状態（Settings） ----

@pytest.mark.parametrize("raw,status,plan,fetch", [
    ("free", "confirmed", "free", "free"), ("light", "confirmed", "light", "light"),
    ("standard", "confirmed", "standard", "standard"), ("premium", "confirmed", "premium", "premium"),
    (" Light ", "confirmed", "light", "light"), ("PREMIUM", "confirmed", "premium", "premium"),
    (None, "unset", "", "free"), ("", "unset", "", "free"), ("   ", "unset", "", "free"),
    ("gold", "invalid", "", None), ("frее", "invalid", "", None),  # 全角・似た文字も Free にしない
])
def test_plan_status(monkeypatch, raw, status, plan, fetch):
    _env(monkeypatch, JQUANTS_PLAN=raw)
    st = _st()
    assert (st.jquants_plan_status, st.jquants_plan, st.jquants_fetch_plan) == (status, plan, fetch)
    assert st.jquants_plan != "free" or status == "confirmed"  # 未設定・不正は内部でも Free と区別する
    config.get_settings.cache_clear()


def test_status_cannot_be_forced_from_environment(monkeypatch):
    _env(monkeypatch, JQUANTS_PLAN=None, JQUANTS_PLAN_STATUS="confirmed", JQUANTS_PLAN_INVALID_VALUE="x")
    assert _st().jquants_plan_status == "unset" and _st().jquants_plan_invalid_value == ""
    config.get_settings.cache_clear()


def test_invalid_value_is_sanitized_and_short(monkeypatch):
    _env(monkeypatch, JQUANTS_PLAN="x" * 100)
    assert _st().jquants_plan_invalid_value == "x" * 32
    config.get_settings.cache_clear()


# ---- 取得の処理は従来どおり（未設定は Free 相当の安全側の条件） ----

def test_fetch_conditions_unchanged_for_unset_and_confirmed(monkeypatch):
    for raw, expect_plan in ((None, "free"), ("free", "free"), ("light", "light")):
        _env(monkeypatch, JQUANTS_PLAN=raw)
        st = _st()
        assert st.jquants_fetch_plan == expect_plan
        assert window_dates(TODAY, st.jquants_fetch_plan, set(), 3) == window_dates(TODAY, expect_plan, set(), 3)
    _env(monkeypatch, JQUANTS_PLAN=None)
    assert window_dates(TODAY, _st().jquants_fetch_plan, set(), 1)[0] <= TODAY - timedelta(days=84)  # 従来の安全側の範囲
    config.get_settings.cache_clear()


class _Recorder:
    created: list = []

    def __init__(self, api_key, plan="free", network_secret=False):
        _Recorder.created.append(plan)

    def topix(self, start=None):
        return []

    def daily_bars_by_date(self, d):
        return []

    def fins_summary(self, code=None, disc_date=None):
        return []

    def close(self):
        pass


def test_unset_plan_fetches_with_free_equivalent_client(client, monkeypatch):
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN=None)
    _Recorder.created = []
    monkeypatch.setattr(jquants, "JQuantsClient", _Recorder)
    with SessionLocal() as s:
        r = ingest_jquants.run(s)
        assert r.status == "success" and _Recorder.created == ["free"]  # アクセス間隔など、取得の条件は従来どおり
        assert "プラン未設定・Free相当の条件" in r.source and "（free）" not in r.source  # 記録では Free と区別する
    config.get_settings.cache_clear()


# ---- 不正な値：アプリは起動し、J-Quants の取得だけを止める ----

def test_invalid_plan_stops_only_jquants(client, monkeypatch, caplog):
    _env(monkeypatch, NETWORK_SECRETS="jquants,tiingo", JQUANTS_PLAN="gold", US_PRICE_PROVIDER="tiingo")
    _Recorder.created = []
    monkeypatch.setattr(jquants, "JQuantsClient", _Recorder)
    with caplog.at_level(logging.ERROR), SessionLocal() as s:
        r1 = ingest_jquants.run(s)
        r2 = ingest_jquants_fins.run(s)
    assert _Recorder.created == []  # API に接続しない（クライアントも作らない）
    for r in (r1, r2):
        assert r.status == "skipped" and "設定不備" in r.message and "gold" in r.message
    assert sum("設定不備" in m or "J-Quants の" in m for m in caplog.messages) >= 2  # ログで知らせる
    # アプリ全体は起動し、ほかのデータの表示・判定は通常どおり
    for u in ("/api/v1/system/status", "/api/v1/market/overview", "/api/v1/securities?query=a"):
        assert client.get(u).status_code == 200
    provs = {p["key"]: p for p in client.get("/api/v1/system/status").json()["providers"]}
    assert provs["jquants"]["status"] == "error" and "設定不備" in provs["jquants"]["message"]
    assert provs["us_prices"]["status"] == "ok" and provs["sec_edgar"]["status"] in ("ok", "not_connected")
    assert provs["fred"]["status"] == "ok" and provs["edinet"]["status"] in ("ok", "not_connected")
    from app.services import ingest_us_prices
    assert ingest_us_prices.provider_chain(_st()) == ["tiingo"]  # 米国株の取得の設定は影響を受けない
    ds = {d["dataset"]: d for d in client.get("/api/v1/system/status").json()["data_sources"]}
    for key in ("prices_jp", "financials_jp"):
        assert "設定不備" in ds[key]["delay"] and any("設定不備" in w for w in ds[key]["warnings"])
        assert not any("Free" in w for w in ds[key]["warnings"])
    assert "設定不備" in ds["prices_jp"]["price_state_label"]
    config.get_settings.cache_clear()


def test_app_and_worker_start_with_invalid_plan(tmp_path, monkeypatch):
    """不正な値でも、API の起動（設定の読み込み・起動処理）とワーカーのほかの取得が止まらない。"""
    from fastapi.testclient import TestClient
    from app import db as dbmod
    from app import worker
    _env(monkeypatch, JQUANTS_PLAN="gold", DATABASE_URL=f"sqlite:///{tmp_path / 't.sqlite3'}")
    eng = dbmod.make_engine(f"sqlite:///{tmp_path / 't.sqlite3'}", create=True)
    monkeypatch.setattr(dbmod, "engine", eng)
    dbmod.SessionLocal.configure(bind=eng)
    from app.main import app
    with TestClient(app) as c:  # 起動処理（lifespan）を含めて起動する
        assert c.get("/api/v1/system/status").status_code == 200
    ran = []
    monkeypatch.setattr(worker, "job", lambda name, **kw: ran.append(name))
    monkeypatch.setattr(sys, "argv", ["worker", "fred"])
    worker.main()
    assert ran == ["fred"]
    config.get_settings.cache_clear()


# ---- 日本企業の財務データの遅延の表示・判定 ----

def _jp_company_with_fins(disc_days_ago=100):
    with SessionLocal() as s:
        x = Security(market="JP", code="7203", name="トヨタ自動車", name_search="とよた", security_type="common",
                     is_active=True, source="t", fetched_at=datetime.now(timezone.utc))
        s.add(x)
        s.commit()
        d = (TODAY - timedelta(days=disc_days_ago)).isoformat()
        store_records(s, [rec("D1", "72030", d, "FYFinancialStatements_Consolidated_JP", "FY", "2025-04-01", "2026-03-31",
                              "2025-04-01", "2026-03-31", Sales=1000, OP=100, NP=50, TA=5000, Eq=2000)],
                      {"7203": x.id}, datetime.now(timezone.utc))
        s.commit()


@pytest.mark.parametrize("raw,expect", [
    ("free", "Freeプランのため、決算短信は開示から約12週間後"), ("light", None), ("standard", None), ("premium", None),
    (None, "プランが未確認"), ("gold", "設定不備")])
def test_financials_delay_note_by_plan(client, monkeypatch, raw, expect):
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN=raw)
    _jp_company_with_fins()
    f = client.get("/api/v1/securities/JP/7203/financials").json()
    if expect is None:
        assert f.get("delay_note") is None
    else:
        assert expect in f["delay_note"]
    if raw != "free":  # 確認済みの Free 以外では、Freeプランと表示せず、12週間も表示しない（取得の条件が「Free 相当」とだけ示す）
        assert "Freeプラン" not in (f.get("delay_note") or "") and "12週間" not in (f.get("delay_note") or "")
    config.get_settings.cache_clear()


@pytest.mark.parametrize("raw,delay", [("free", "約12週間遅延（Freeプラン）"), ("light", "前営業日までのデータ（Lightプラン）"),
                                       (None, "プラン未確認"), ("gold", "設定不備")])
def test_financials_dataset_delay_and_warnings(client, monkeypatch, raw, delay):
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN=raw)
    d = {x["dataset"]: x for x in client.get("/api/v1/system/status").json()["data_sources"]}["financials_jp"]
    assert delay in d["delay"]
    joined = " ".join(d["warnings"])
    assert ("約12週間後" in joined) == (raw == "free")  # Free の注意は、確認済みの Free のときだけ
    if raw is None:
        assert "プランが未確認" in joined and "Free 相当の安全側の条件" in joined and "12週間" not in joined
    config.get_settings.cache_clear()


def _fin(client):
    return client.get("/api/v1/securities/JP/7203/financials").json()


@pytest.mark.parametrize("plan", PLANS)
def test_financials_freshness_uses_confirmed_plan_delay(client, monkeypatch, plan):
    """確認済みのプラン：そのプランの遅延（providers/jquants.py の表）で鮮度を判定する（最新の開示から 120日＋遅延）。"""
    _jp_company_with_fins(disc_days_ago=150)
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN=plan)
    days = 120 + jquants.DELAY_DAYS.get(plan, 0)
    f = _fin(client)
    assert f["freshness_days"] == days
    assert (f["freshness"], f["data_status"]) == (("fresh", "ok") if 150 <= days else ("stale", "stale"))
    if plan == "free":
        assert f["freshness"] == "fresh"   # Free：120＋84日以内
    else:
        assert f["freshness"] == "stale"   # Light・Standard・Premium：遅延なし → 120日超は古い
    config.get_settings.cache_clear()


@pytest.mark.parametrize("plan", PLANS)
def test_financials_freshness_boundaries_per_plan(client, monkeypatch, plan):
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN=plan)
    days = 120 + jquants.DELAY_DAYS.get(plan, 0)
    _jp_company_with_fins(disc_days_ago=days)          # ちょうど基準の日数：古くない
    assert _fin(client)["freshness"] == "fresh"
    with SessionLocal() as s:  # 開示日を1日だけ古くする（テスト用DBの中だけ）
        from app.models import FinancialFact
        for f in s.query(FinancialFact).all():
            f.filed = f.filed - timedelta(days=1)
        s.commit()
    assert _fin(client)["freshness"] == "stale"        # 基準の日数を1日超えたら古い
    config.get_settings.cache_clear()


@pytest.mark.parametrize("raw", [None, "", "gold"])
@pytest.mark.parametrize("age", [10, 150, 400])
def test_financials_freshness_unknown_when_plan_unconfirmed_or_invalid(client, monkeypatch, raw, age):
    """未設定・設定不備：開示がどれだけ新しく・古くても、古い・古くないのどちらにも断定しない。
    財務データ・最終開示日・取得日時・注意書きは表示する。"""
    _jp_company_with_fins(disc_days_ago=age)
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN=raw)
    f = _fin(client)
    assert f["freshness"] == "unknown" and f["data_status"] == "unknown" and f["freshness_days"] is None
    assert f["annual"] and f["annual"][-1]["values"]["revenue"] == 1000             # 財務データの有無（値は表示する）
    assert f["latest_filed"] == (TODAY - timedelta(days=age)).isoformat()           # 最終開示日
    assert f["fetched_at"] is not None                                              # 取得日時
    assert ("プランが未確認" if raw != "gold" else "設定不備") in f["delay_note"]      # 注意書き
    config.get_settings.cache_clear()


def test_invalid_plan_does_not_affect_us_financials_or_other_providers(client, monkeypatch):
    """設定不備でも、米国企業の財務の鮮度判定・ほかのデータ連携の状態は通常どおり。"""
    from app.models import FinancialFact
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN="gold")
    with SessionLocal() as s:
        x = Security(market="US", code="AAA", cik=1, name="AAA Inc", name_search="aaa", security_type="common", is_active=True,
                     source="t", fetched_at=datetime.now(timezone.utc))
        s.add(x)
        s.flush()
        for concept, tag, v in (("revenue", "Revenues", 1000.0), ("net_income", "NetIncomeLoss", 100.0)):
            s.add(FinancialFact(security_id=x.id, concept=concept, tag=tag, unit="USD", period_start=date(2025, 1, 1),
                                period_end=date(2025, 12, 31), form="10-K", accn="a", filed=TODAY - timedelta(days=30),
                                value=v, source="SEC EDGAR XBRL", fetched_at=datetime.now(timezone.utc)))
        s.commit()
    f = client.get("/api/v1/securities/US/AAA/financials").json()
    assert f["freshness"] == "fresh" and f["data_status"] == "ok" and f["freshness_days"] == 120
    provs = {p["key"]: p for p in client.get("/api/v1/system/status").json()["providers"]}
    assert provs["jquants"]["status"] == "error" and provs["fred"]["status"] == "ok"
    config.get_settings.cache_clear()


# ---- 株価（PR #10）：未確認・設定不備 ----

def test_price_display_unconfirmed_and_invalid(client, monkeypatch):
    from app.models import DailyPrice
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN=None)
    with SessionLocal() as s:
        x = Security(market="JP", code="7203", name="t", name_search="t", security_type="common", is_active=True,
                     source="t", fetched_at=datetime.now(timezone.utc))
        s.add(x)
        s.flush()
        s.add(DailyPrice(security_id=x.id, trade_date=TODAY - timedelta(days=85), close=1.0, provider="jquants",
                         fetched_at=datetime.now(timezone.utc)))
        s.add(IngestionRun(dataset="prices_jp", source="t", status="success", rows=1, finished_at=datetime.now(timezone.utc)))
        s.commit()
    q = client.get("/api/v1/securities/JP/7203").json()["quote"]
    assert q["price_state_label"] == "取得済み（プラン未確認・遅延を判定できません）" and "Free" not in q["note"]
    d = {x["dataset"]: x for x in client.get("/api/v1/system/status").json()["data_sources"]}["prices_jp"]
    assert "プラン未確認" in d["delay"] and "12週間" not in " ".join(d["warnings"])
    _env(monkeypatch, JQUANTS_PLAN="gold")
    q = client.get("/api/v1/securities/JP/7203").json()["quote"]
    assert q["price_state"] == "ok" and q["price_state_label"] == "取得済み（設定不備・遅延を判定できません）・設定不備"
    assert "設定不備" in q["note"] and "Free" not in q["note"]
    config.get_settings.cache_clear()


# ---- TOPIX ----

def _topix(state_env, bars=None, record=None, monkeypatch=None, client=None):
    _env(monkeypatch, **state_env)
    with SessionLocal() as s:
        for d, v in (bars or []):
            s.add(IndexBar(index_key="topix", trade_date=d, close=v, provider="jquants", fetched_at=datetime.now(timezone.utc)))
        if record:
            jquants_plan.save_topix_result(s, 1, record, error="HTTP 500" if record == "error" else None)
        s.commit()
    return {q["key"]: q for q in client.get("/api/v1/market/overview").json()}["topix"]


def test_topix_not_configured(client, monkeypatch):
    t = _topix({"NETWORK_SECRETS": "", "JQUANTS_PLAN": "free"}, monkeypatch=monkeypatch, client=client)
    assert t["index_state"] == "not_configured" and "APIキー未設定" in t["note"] and t["value"] is None
    config.get_settings.cache_clear()


def test_topix_free_confirmed_is_excluded_by_verified_spec(client, monkeypatch):
    t = _topix({"NETWORK_SECRETS": "jquants", "JQUANTS_PLAN": "free"}, monkeypatch=monkeypatch, client=client)
    assert t["index_state"] == "plan_excludes" and "Freeプランでは TOPIX は提供されていません" in t["note"]
    assert "API の応答で確認" in t["note"] and "未検証" not in t["note"]
    config.get_settings.cache_clear()


@pytest.mark.parametrize("plan", ["light", "standard", "premium"])
def test_topix_paid_plan_not_fetched_marks_spec_unverified(client, monkeypatch, plan):
    t = _topix({"NETWORK_SECRETS": "jquants", "JQUANTS_PLAN": plan}, monkeypatch=monkeypatch, client=client)
    assert t["index_state"] == "not_fetched" and "未取得" in t["note"] and "公式案内・未検証" in t["note"]
    assert "APIキー未設定" not in t["note"]  # キーがあるのに「未設定」と表示しない（従来の誤り）
    config.get_settings.cache_clear()


def test_topix_unconfirmed_plan(client, monkeypatch):
    t = _topix({"NETWORK_SECRETS": "jquants", "JQUANTS_PLAN": None}, monkeypatch=monkeypatch, client=client)
    assert t["index_state"] == "plan_unconfirmed" and "プランが未確認" in t["note"]
    assert "Free" not in t["note"] and "提供されていません" not in t["note"]
    config.get_settings.cache_clear()


def test_topix_invalid_plan(client, monkeypatch):
    t = _topix({"NETWORK_SECRETS": "jquants", "JQUANTS_PLAN": "gold"}, monkeypatch=monkeypatch, client=client)
    assert t["index_state"] == "plan_invalid" and t["data_status"] == "error" and "設定不備" in t["note"]
    config.get_settings.cache_clear()


@pytest.mark.parametrize("plan", [None, "light"])
def test_topix_api_reported_plan_not_available(client, monkeypatch, plan):
    """API がプランの範囲外と応答した事実は、プランの設定（未確認・Light）より優先して示す。"""
    t = _topix({"NETWORK_SECRETS": "jquants", "JQUANTS_PLAN": plan}, record="plan_not_available",
               monkeypatch=monkeypatch, client=client)
    assert t["index_state"] == "plan_not_available" and "API の応答" in t["note"] and t["value"] is None
    config.get_settings.cache_clear()


def test_topix_general_fetch_error_is_distinct(client, monkeypatch):
    t = _topix({"NETWORK_SECRETS": "jquants", "JQUANTS_PLAN": "light"}, record="error", monkeypatch=monkeypatch, client=client)
    assert t["index_state"] == "fetch_error" and t["data_status"] == "error" and "取得に失敗" in t["note"]
    assert "プラン" not in t["note"].split("。")[0]
    config.get_settings.cache_clear()


def test_topix_with_data_confirmed_and_unconfirmed(client, monkeypatch):
    bars = [(TODAY - timedelta(days=1), 2800.0), (TODAY - timedelta(days=2), 2750.0)]
    t = _topix({"NETWORK_SECRETS": "jquants", "JQUANTS_PLAN": "light"}, bars=bars, monkeypatch=monkeypatch, client=client)
    assert t["index_state"] == "ok" and t["value"] == 2800.0 and "Lightプラン" in t["note"] and t["change_pct"] is not None
    _env(monkeypatch, JQUANTS_PLAN=None)
    t = {q["key"]: q for q in client.get("/api/v1/market/overview").json()}["topix"]
    assert t["index_state"] == "ok" and "プラン未確認" in t["note"] and "Free" not in t["note"]
    config.get_settings.cache_clear()


def test_topix_fetch_records_structured_results(client, monkeypatch):
    """取得の処理：TOPIX の結果（成功／API がプランの範囲外と応答／そのほかの失敗）を構造化して残す。"""
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN="free")

    class PlanOut(_Recorder):
        def topix(self, start=None):
            raise jquants.PlanNotAvailable("/v2/indices/bars/daily/topix はご契約のプランでは提供されていません")

    class Broken(_Recorder):
        def topix(self, start=None):
            raise RuntimeError("通信エラー：ConnectError https://api.jquants.com/...")

    class Ok(_Recorder):
        def topix(self, start=None):
            return [{"Date": "2026-08-01", "C": 2800.0}]

    for cls, expect in ((PlanOut, "plan_not_available"), (Broken, "error"), (Ok, "ok")):
        monkeypatch.setattr(jquants, "JQuantsClient", cls)
        with SessionLocal() as s:
            r = ingest_jquants.run(s)
            rec_ = jquants_plan.last_topix_result(s)
            assert rec_["result"] == expect and rec_["run_id"] == r.id and rec_["plan_status"] == "confirmed"
    with SessionLocal() as s:
        assert s.query(DataQualityCheck).filter_by(dataset=jquants_plan.RUN_RECORD).count() == 3
    config.get_settings.cache_clear()
