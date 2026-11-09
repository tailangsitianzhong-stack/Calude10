"""銘柄を指定した株価の取得（jquants --codes / us_prices --codes）のテスト。値はテスト用。

外部APIには接続しない（偽のクライアント）。DB はテスト用の一時的な SQLite（tests/conftest.py）。
"""
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

from app import config
from app.db import SessionLocal
from app.models import DailyPrice, DataQualityCheck, FinancialFact, IndexBar, IngestionRun, Security
from app.providers import jquants, us_prices
from app.services import ingest_jquants, ingest_us_prices

TODAY = date.today()


def _env(monkeypatch, **kv):
    for k, v in kv.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()


def _sec(market, code, fin=False):
    with SessionLocal() as s:
        x = Security(market=market, code=code, name=code, name_search=code.lower(), security_type="common", is_active=True,
                     source="t", fetched_at=datetime.now(timezone.utc))
        s.add(x)
        s.flush()
        if fin:
            s.add(FinancialFact(security_id=x.id, concept="revenue", tag="Revenues", unit="USD", period_start=date(2025, 1, 1),
                                period_end=date(2025, 12, 31), form="10-K", accn=f"a{code}", filed=date(2026, 2, 1), value=1,
                                source="t", fetched_at=datetime.now(timezone.utc)))
        s.commit()
        return x.id


def _rows_by_code(sid=None, provider=None):
    with SessionLocal() as s:
        q = s.query(DailyPrice)
        if provider:
            q = q.filter(DailyPrice.provider == provider)
        return [(p.security_id, p.trade_date, p.close) for p in q.order_by(DailyPrice.trade_date)]


def _weekdays(end, n):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


# ---- 日本株（J-Quants） ----

class JQ:
    """J-Quants の偽のクライアント。呼び出しを記録する。銘柄ごとの日足は、指定と違う銘柄の行も混ぜて返す（防御の確認）。"""
    calls: list = []
    created = 0
    by_code_rows = None  # None なら期間の平日すべてを返す

    def __init__(self, api_key, plan="free", network_secret=False):
        JQ.created += 1

    def topix(self, start=None):
        JQ.calls.append(("topix",))
        return []

    def daily_bars_by_date(self, d):
        JQ.calls.append(("by_date", d))
        return [{"Code": "72030", "Date": d.isoformat(), "C": 1.0}, {"Code": "67580", "Date": d.isoformat(), "C": 2.0}]

    def daily_bars_by_code(self, code, start=None, end=None):
        JQ.calls.append(("by_code", code, start, end))
        if JQ.by_code_rows is not None:
            return JQ.by_code_rows
        days = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        return ([{"Code": "72030", "Date": x.isoformat(), "O": 1, "H": 2, "L": 0.5, "C": 1000.0 + i, "Vo": 10}
                 for i, x in enumerate(days)]
                + [{"Code": "67580", "Date": end.isoformat(), "C": 9.0}])  # 指定外の銘柄の行（保存してはいけない）

    def close(self):
        pass


@pytest.fixture()
def jq(client, monkeypatch):
    _env(monkeypatch, NETWORK_SECRETS="jquants", JQUANTS_PLAN="free")
    JQ.calls, JQ.created, JQ.by_code_rows = [], 0, None
    monkeypatch.setattr(jquants, "JQuantsClient", JQ)
    ids = {c: _sec("JP", c) for c in ("7203", "6758", "9984")}
    yield ids
    config.get_settings.cache_clear()


def test_jp_codes_fetches_and_stores_only_7203(jq):
    with SessionLocal() as s:
        r = ingest_jquants.run(s, codes=["7203"], days=10)
    assert r.status == "success" and r.rows == 10 and "銘柄指定 7203" in r.source
    assert [c[0] for c in JQ.calls] == ["by_code"] and JQ.calls[0][1] == "7203"  # TOPIX・日付ごとの全銘柄は呼ばない
    rows = _rows_by_code()
    assert {sid for sid, _, _ in rows} == {jq["7203"]} and len(rows) == 10       # 7203 以外は保存しない
    with SessionLocal() as s:
        assert s.query(IndexBar).count() == 0 and s.query(DataQualityCheck).count() == 0  # TOPIX の取得・記録もしない


def test_jp_codes_window_matches_existing_fetch_conditions(jq):
    """期間：最新＝今日−プランの遅延（Free 84日）、直近の平日 days 日。上限は全銘柄の取得と同じ（Free 40日）。"""
    newest = TODAY - timedelta(days=84)
    with SessionLocal() as s:
        ingest_jquants.run(s, codes=["7203"], days=5)
    _, code, start, end = JQ.calls[0]
    assert end == newest and start == _weekdays(newest, 5)[-1]
    JQ.calls = []
    with SessionLocal() as s:
        ingest_jquants.run(s, codes=["7203"], days=1000)  # 上限を超える指定は上限（40日）まで
    _, _, start, end = JQ.calls[0]
    assert start == _weekdays(newest, 40)[-1]
    assert all(d <= newest for _, d, _ in _rows_by_code())  # 遅延より新しい日は取得しない


@pytest.mark.parametrize("bad", [["72O3"], ["720"], ["72031"], ["7203;DROP"], [""], []])
def test_jp_invalid_code_stops_before_api_and_db(jq, bad):
    with pytest.raises(ValueError):
        with SessionLocal() as s:
            ingest_jquants.run(s, codes=bad)
    assert JQ.created == 0 and JQ.calls == []
    with SessionLocal() as s:
        assert s.query(IngestionRun).count() == 0  # 実行の記録も作らない


def test_jp_unknown_code_stops_without_api(jq):
    with SessionLocal() as s:
        r = ingest_jquants.run(s, codes=["1111"])  # 形式は正しいが銘柄一覧にない
    assert r.status == "failed" and "銘柄一覧にない" in r.message and JQ.created == 0 and _rows_by_code() == []


def test_jp_zero_rows_is_not_success(jq):
    JQ.by_code_rows = []
    with SessionLocal() as s:
        r = ingest_jquants.run(s, codes=["7203"], days=5)
    assert r.status == "failed" and r.rows == 0 and "0件" in r.message


def test_jp_codes_never_overwrites_existing_rows(jq):
    newest = TODAY - timedelta(days=84)
    d0 = _weekdays(newest, 1)[0]
    with SessionLocal() as s:
        s.add(DailyPrice(security_id=jq["7203"], trade_date=d0, close=1.23, provider="jquants", fetched_at=datetime.now(timezone.utc)))
        s.commit()
    with SessionLocal() as s:
        r = ingest_jquants.run(s, codes=["7203"], days=3)
    rows = {d: c for _, d, c in _rows_by_code()}
    assert rows[d0] == 1.23 and len(rows) == 3 and r.rows == 2  # 既存の行はそのまま、新しい2日だけ追加


def test_jp_without_codes_keeps_full_market_behavior(jq):
    with SessionLocal() as s:
        r = ingest_jquants.run(s)
    kinds = [c[0] for c in JQ.calls]
    assert kinds[0] == "topix" and "by_date" in kinds and "by_code" not in kinds  # 従来どおり：TOPIX＋日付ごとの全銘柄
    assert len([k for k in kinds if k == "by_date"]) == 40                       # Free の1回の日数（従来どおり）
    assert {sid for sid, _, _ in _rows_by_code()} == {jq["7203"], jq["6758"]}
    assert "銘柄指定" not in r.source


# ---- 米国株（Tiingo / Massive） ----

class Tiingo:
    asked: list = []
    created = 0
    rows_for = None   # {ticker: rows} を指定できる
    fail = None       # 例外を出す

    def __init__(self, *a, **k):
        Tiingo.created += 1

    def daily(self, ticker, start):
        Tiingo.asked.append(ticker)
        if Tiingo.fail:
            raise Tiingo.fail
        if Tiingo.rows_for is not None:
            return Tiingo.rows_for.get(ticker, [])
        d = TODAY - timedelta(days=3)
        return [{"code": ticker, "date": d, "close": 200.0}, {"code": "MSFT", "date": d, "close": 1.0}]  # 指定外を混ぜる

    def close(self):
        pass


class MassiveForbidden:
    created = 0

    def __init__(self, *a, **k):
        MassiveForbidden.created += 1
        raise AssertionError("Massive を呼び出してはいけない")


class MassiveOK:
    created = 0

    def __init__(self, *a, **k):
        MassiveOK.created += 1

    def grouped_daily(self, d):
        return [{"code": "AAPL", "date": d, "close": 1.0}, {"code": "MSFT", "date": d, "close": 2.0}]

    def close(self):
        pass


@pytest.fixture()
def us(client, monkeypatch):
    _env(monkeypatch, NETWORK_SECRETS="tiingo,massive", US_PRICE_PROVIDER="tiingo", US_PRICE_FALLBACK=None, MASSIVE_MAX_DAYS="2")
    Tiingo.asked, Tiingo.created, Tiingo.rows_for, Tiingo.fail = [], 0, None, None
    MassiveForbidden.created, MassiveOK.created = 0, 0
    monkeypatch.setattr(us_prices, "TiingoClient", Tiingo)
    monkeypatch.setattr(us_prices, "MassiveClient", MassiveForbidden)
    ids = {c: _sec("US", c, fin=True) for c in ("AAPL", "MSFT", "KO")}
    yield ids
    config.get_settings.cache_clear()


def test_us_codes_fetches_and_stores_only_aapl(us):
    with SessionLocal() as s:
        r = ingest_us_prices.run(s, codes=["aapl"])
    assert r.status == "success" and r.rows == 1 and "銘柄指定 AAPL" in r.source and "Massive" not in r.source
    assert Tiingo.asked == ["AAPL"] and MassiveForbidden.created == 0           # 他の銘柄・Massive は呼ばない
    assert {sid for sid, _, _ in _rows_by_code()} == {us["AAPL"]}                 # AAPL 以外は保存しない


@pytest.mark.parametrize("bad", [["AA PL"], ["1AAPL"], ["AAPL;"], ["TOOLONGTICKER"], [""], []])
def test_us_invalid_ticker_stops_before_api_and_db(us, bad):
    with pytest.raises(ValueError):
        with SessionLocal() as s:
            ingest_us_prices.run(s, codes=bad)
    assert Tiingo.created == 0 and MassiveForbidden.created == 0
    with SessionLocal() as s:
        assert s.query(IngestionRun).count() == 0


def test_us_unknown_ticker_stops_without_api(us):
    with SessionLocal() as s:
        r = ingest_us_prices.run(s, codes=["ZZZZ"])
    assert r.status == "failed" and "銘柄一覧にない" in r.message and Tiingo.created == 0 and MassiveForbidden.created == 0


def test_us_zero_rows_is_not_success_and_no_massive(us):
    Tiingo.rows_for = {}
    with SessionLocal() as s:
        r = ingest_us_prices.run(s, codes=["AAPL"])
    assert r.status == "failed" and r.rows == 0 and "0件" in r.message and MassiveForbidden.created == 0


@pytest.mark.parametrize("err", [PermissionError("Tiingo の認証に失敗しました（401）"), RuntimeError("通信エラー：ConnectError")])
def test_us_codes_tiingo_failure_never_switches_to_massive(us, err):
    Tiingo.fail = err
    with SessionLocal() as s:
        r = ingest_us_prices.run(s, codes=["AAPL"])
    assert r.status == "failed" and MassiveForbidden.created == 0 and _rows_by_code() == []
    assert "代替の取得元は使わない" in r.message


def test_us_codes_without_tiingo_credential_stops_and_does_not_use_massive(us, monkeypatch):
    _env(monkeypatch, NETWORK_SECRETS="massive", US_PRICE_PROVIDER="massive")
    with SessionLocal() as s:
        r = ingest_us_prices.run(s, codes=["AAPL"])
    assert r.status == "failed" and "Tiingo" in r.message and MassiveForbidden.created == 0 and Tiingo.created == 0


def test_no_fallback_in_full_mode_does_not_switch(us):
    Tiingo.fail = PermissionError("Tiingo の認証に失敗しました（401）")
    with SessionLocal() as s:
        r = ingest_us_prices.run(s, no_fallback=True)
    assert r.status == "failed" and MassiveForbidden.created == 0 and "代替への切り替えなし" in r.source


def test_full_mode_without_flags_keeps_existing_fallback(us, monkeypatch):
    """指定なし：従来どおり（Tiingo が使えなければ Massive に切り替える）。"""
    monkeypatch.setattr(us_prices, "MassiveClient", MassiveOK)
    Tiingo.fail = PermissionError("Tiingo の認証に失敗しました（401）")
    with SessionLocal() as s:
        r = ingest_us_prices.run(s)
    assert r.status == "success" and MassiveOK.created == 1 and "切り替え" in r.source


def test_full_mode_without_codes_requests_sec_universe_as_before(us):
    with SessionLocal() as s:
        r = ingest_us_prices.run(s)
    assert sorted(Tiingo.asked) == ["AAPL", "KO", "MSFT"] and r.status == "success"
    assert "銘柄指定" not in r.source


def test_us_codes_never_overwrites_existing_rows(us):
    d0 = TODAY - timedelta(days=3)
    with SessionLocal() as s:
        s.add(DailyPrice(security_id=us["AAPL"], trade_date=d0 - timedelta(days=1), close=7.0, provider="tiingo",
                         fetched_at=datetime.now(timezone.utc)))
        s.commit()
    Tiingo.rows_for = {"AAPL": [{"code": "AAPL", "date": d0 - timedelta(days=1), "close": 999.0},
                                {"code": "AAPL", "date": d0, "close": 200.0}]}
    with SessionLocal() as s:
        r = ingest_us_prices.run(s, codes=["AAPL"])
    rows = {d: c for _, d, c in _rows_by_code()}
    assert rows[d0 - timedelta(days=1)] == 7.0 and rows[d0] == 200.0 and r.rows == 1


def test_us_codes_cross_check_only_for_specified(us):
    """銘柄指定の実行では、他の銘柄の取得元どうしの照合の記録を作らない。"""
    d = TODAY - timedelta(days=5)
    with SessionLocal() as s:
        for p, c in (("tiingo", 100.0), ("massive", 150.0)):
            s.add(DailyPrice(security_id=us["MSFT"], trade_date=d, close=c, provider=p, fetched_at=datetime.now(timezone.utc)))
        s.commit()
    with SessionLocal() as s:
        ingest_us_prices.run(s, codes=["AAPL"])
        assert s.query(DataQualityCheck).filter_by(dataset="prices_us").count() == 0


# ---- コマンドライン（worker） ----

@pytest.mark.parametrize("argv", [
    ["jquants", "--codes", "72O3"], ["jquants", "--codes", ""], ["us_prices", "--codes", "AA PL"],
    ["jquants", "--days", "5"], ["jquants", "--codes", "7203", "--days", "0"], ["us_prices", "--days", "3"],
    ["jquants", "--no-fallback"],
])
def test_worker_rejects_invalid_options_before_anything(monkeypatch, argv):
    from app import worker
    called = []
    monkeypatch.setattr(worker, "init_db", lambda *a, **k: called.append("init_db"))
    monkeypatch.setattr(worker, "job", lambda *a, **k: called.append("job"))
    monkeypatch.setattr(sys, "argv", ["worker", *argv])
    with pytest.raises(SystemExit) as e:
        worker.main()
    assert e.value.code == 2 and called == []


@pytest.mark.parametrize("argv,expect", [
    (["jquants", "--codes", "7203", "--days", "40"], ("jquants", {"codes": ["7203"], "days": 40})),
    (["jquants", "--codes", "7203"], ("jquants", {"codes": ["7203"], "days": None})),
    (["us_prices", "--codes", "AAPL"], ("us_prices", {"codes": ["AAPL"], "no_fallback": False})),
    (["us_prices", "--no-fallback"], ("us_prices", {"codes": None, "no_fallback": True})),
    (["jquants"], ("jquants", {})), (["us_prices"], ("us_prices", {})),  # 指定なしは従来どおり
])
def test_worker_passes_options(monkeypatch, argv, expect):
    from app import worker
    seen = []
    monkeypatch.setattr(worker, "init_db", lambda *a, **k: None)
    monkeypatch.setattr(worker, "job", lambda name, **kw: seen.append((name, kw)))
    monkeypatch.setattr(sys, "argv", ["worker", *argv])
    worker.main()
    assert seen == [expect]
