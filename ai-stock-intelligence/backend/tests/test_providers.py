"""プロバイダーの解析処理のテスト。

ここで使う応答データは、各APIの公式仕様（J-Quants V2 のレスポンスサンプル等）の書式を再現したテスト用の値であり、
実データとして画面に出ることはない。実際の接続確認は、APIキーを設定した環境で `python -m app.worker ...` で行う。
"""
from datetime import date

from app.providers import fred, jquants, sec, us_prices
from app.services.ingest_sec import float_periods, normalize_ticker
from app.services.quality import outliers, staleness


def test_fred_csv_skips_missing_values():
    rows, missing = fred.parse_csv("observation_date,DGS10\n2026-10-30,4.10\n2026-11-02,.\n2026-11-03,4.12\n")
    assert rows == [(date(2026, 10, 30), 4.10), (date(2026, 11, 3), 4.12)] and missing == 1


def test_fred_csv_rejects_unexpected_format():
    import pytest
    with pytest.raises(ValueError):
        fred.parse_csv("<html>error</html>")


def test_jquants_v2_bars_short_column_names_and_codes():
    rows = jquants.parse_daily_bars([
        {"Date": "2023-03-24", "Code": "86970", "O": 2047.0, "H": 2069.0, "L": 2035.0, "C": 2045.0, "UL": "0", "LL": "0",
         "Vo": 2202500.0, "Va": 4507051850.0, "AdjFactor": 1.0, "AdjC": 2045.0, "MktCap": 1000.0},
        {"Date": "2023-03-24", "Code": "130A0", "O": None, "H": None, "L": None, "C": None, "Vo": None, "Va": None},  # 約定なし
    ])
    assert len(rows) == 1 and rows[0]["code"] == "8697" and rows[0]["close"] == 2045.0 and rows[0]["volume"] == 2202500.0
    assert jquants.to_jq_code("7203") == "72030" and jquants.to_jq_code("130A") == "130A0"
    assert jquants.from_jq_code("130A0") == "130A"


def test_jquants_requires_key():
    import pytest
    with pytest.raises(ValueError):
        jquants.JQuantsClient("")


def test_us_price_parsers():
    t = us_prices.parse_tiingo("KO", [{"date": "2026-11-03T00:00:00.000Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                                         "adjClose": 1.4, "volume": 10}])
    assert t[0]["date"] == date(2026, 11, 3) and t[0]["close"] == 1.5
    m = us_prices.parse_massive_grouped({"results": [{"T": "BRK-B", "t": 1762203600000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9}]})
    assert m[0]["code"] == "BRK.B" and m[0]["close"] == 1.5


def test_sec_extract_facts_filters_forms_and_dedupes():
    cf = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        {"start": "2025-01-01", "end": "2025-12-31", "val": 100, "accn": "a1", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-20"},
        {"start": "2025-01-01", "end": "2025-12-31", "val": 100, "accn": "a1", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-20"},
        {"start": "2025-01-01", "end": "2025-12-31", "val": 100, "accn": "a2", "fy": 2025, "fp": "FY", "form": "8-K", "filed": "2026-02-01"},
    ]}}}}}
    facts = sec.extract_facts(cf)
    assert len(facts) == 1 and facts[0]["concept"] == "revenue"


def test_ticker_normalization_and_float_periods():
    assert normalize_ticker("BRK-B") == "BRK.B"
    assert float_periods(date(2026, 11, 4))[:3] == ["CY2026Q4I", "CY2026Q3I", "CY2026Q2I"]
    assert float_periods(date(2026, 2, 1))[:2] == ["CY2026Q1I", "CY2025Q4I"]


def test_quality_checks():
    assert staleness(date(2026, 10, 1), "daily", date(2026, 11, 4))[0] is True
    assert staleness(date(2026, 9, 1), "monthly", date(2026, 11, 4))[0] is False
    series = [(date(2026, 1, 1 + i % 28), 100 + (i % 3)) for i in range(60)] + [(date(2026, 3, 1), 300)]
    assert outliers(series) and outliers(series)[-1]["date"] == "2026-03-01"


def test_fred_incremental_update_handles_period_start_before_cosd(client, monkeypatch):
    """月次系列で cosd より前の日付（月初）が返っても、重複登録しない（実際に起きた不具合の回帰テスト）。"""
    from app.db import SessionLocal
    from app.models import MacroObservation
    from app.services import ingest_fred

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def fetch(self, fred_id, start=None):
            self.calls += 1
            return [(date(2026, 8, 1), 1.0), (date(2026, 9, 1), 1.1 if self.calls > 1 else 1.0)], 0

        def close(self):
            pass

    with SessionLocal() as s:
        ingest_fred.ensure_series(s)
        c = FakeClient()
        sd = fred.BY_KEY["jp_call"]
        ingest_fred.ingest_series(s, c, sd)
        s.commit()
        ingest_fred.ingest_series(s, c, sd)  # 2回目（増分取得）
        s.commit()
        vals = {o.obs_date: o.value for o in s.query(MacroObservation).filter_by(series_key="jp_call")}
        assert vals == {date(2026, 8, 1): 1.0, date(2026, 9, 1): 1.1}  # 改定は上書き、重複なし
