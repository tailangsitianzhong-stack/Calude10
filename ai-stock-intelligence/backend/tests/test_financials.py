"""財務データの組み立て（年次・TTM・時点指定）のテスト。値はテスト用。"""
from datetime import date, datetime, timezone

from app.db import SessionLocal
from app.models import FinancialFact, Security
from app.services.financials import snapshot


def fact(sid, concept, tag, start, end, form, accn, filed, value, fp="FY"):
    return FinancialFact(security_id=sid, concept=concept, tag=tag, unit="USD", period_start=start, period_end=end,
                         fiscal_year=end.year, fiscal_period=fp, form=form, accn=accn, filed=filed, value=value,
                         source="test", fetched_at=datetime.now(timezone.utc))


def test_snapshot_annual_ttm_and_point_in_time(client):
    with SessionLocal() as s:
        sec = Security(market="US", code="TST", cik=1, name="Test", name_search="test tst", security_type="common",
                       is_active=True, source="test", fetched_at=datetime.now(timezone.utc))
        s.add(sec)
        s.flush()
        d = date
        s.add_all([
            fact(sec.id, "revenue", "Revenues", d(2024, 1, 1), d(2024, 12, 31), "10-K", "k24", d(2025, 2, 20), 100),
            fact(sec.id, "revenue", "Revenues", d(2025, 1, 1), d(2025, 12, 31), "10-K", "k25", d(2026, 2, 20), 120),
            # 一部の売上だけのタグ（小さい値）は無視され、最大の売上が採られる
            fact(sec.id, "revenue", "SalesRevenueNet", d(2025, 1, 1), d(2025, 12, 31), "10-K", "k25", d(2026, 2, 20), 30),
            fact(sec.id, "revenue", "Revenues", d(2026, 1, 1), d(2026, 9, 30), "10-Q", "q3", d(2026, 11, 1), 100, "Q3"),
            fact(sec.id, "revenue", "Revenues", d(2025, 1, 1), d(2025, 9, 30), "10-Q", "q3", d(2026, 11, 1), 90, "Q3"),
            fact(sec.id, "equity", "StockholdersEquity", None, d(2025, 12, 31), "10-K", "k25", d(2026, 2, 20), 50),
        ])
        s.commit()
        snap = snapshot(s, sec)
        assert [a["period_end"] for a in snap["annual"]] == ["2024-12-31", "2025-12-31"]
        assert snap["annual"][-1]["values"]["revenue"] == 120
        assert snap["ttm"]["values"]["revenue"] == 120 + 100 - 90
        # 2026-01-01 時点では2025年度の10-K（2026-02-20提出）はまだ知り得ない
        old = snapshot(s, sec, as_of=d(2026, 1, 1))
        assert old["annual"][-1]["period_end"] == "2024-12-31" and old["ttm"] is None
    r = client.get("/api/v1/securities/US/TST/financials").json()
    assert r["annual"][-1]["values"]["revenue"] == 120 and r["source"].startswith("SEC")


def _co(s, code):
    x = Security(market="US", code=code, cik=abs(hash(code)) % 10**6 + 10, name=code, name_search=code.lower(),
                 security_type="common", is_active=True, source="test", fetched_at=datetime.now(timezone.utc))
    s.add(x)
    s.flush()
    return x


def test_ttm_ignores_trailing_twelve_month_values_in_10q(client):
    """A：10-Q の「過去12か月の合計」（AMZN の営業CF など）を当年の累計と取り違えない。"""
    d = date
    with SessionLocal() as s:
        x = _co(s, "TTMA")
        s.add_all([
            fact(x.id, "cfo", "C", d(2025, 1, 1), d(2025, 12, 31), "10-K", "k", d(2026, 2, 1), 140),
            # 過去12か月の値が先に保存されている場合（AMZN の実データの状態）でも取り違えないこと
            fact(x.id, "cfo", "C", d(2025, 10, 1), d(2026, 9, 30), "10-Q", "q", d(2026, 10, 30), 168, "Q3"),  # 過去12か月
            fact(x.id, "cfo", "C", d(2024, 10, 1), d(2025, 9, 30), "10-Q", "q", d(2026, 10, 30), 131, "Q3"),  # 前年の過去12か月
            fact(x.id, "cfo", "C", d(2026, 1, 1), d(2026, 9, 30), "10-Q", "q", d(2026, 10, 30), 114, "Q3"),   # 当年の累計
            fact(x.id, "cfo", "C", d(2025, 1, 1), d(2025, 9, 30), "10-Q", "q", d(2026, 10, 30), 85, "Q3"),    # 前年同期
        ])
        s.commit()
        t = snapshot(s, x)["ttm"]
    assert t["values"]["cfo"] == 140 + 114 - 85  # ＝168（会社が報告した過去12か月の値と一致）。以前は 140+168−131＝177


def test_ttm_not_computed_from_10q_older_than_latest_10k(client):
    """B：決算期末の後に10-Kだけが出ている時期は、古い10-Qの累計を新しい年度に足さない（AAPL・MU などで二重計上していた）。"""
    d = date
    with SessionLocal() as s:
        x = _co(s, "TTMB")
        s.add_all([
            fact(x.id, "revenue", "Revenues", d(2024, 10, 1), d(2025, 9, 30), "10-K", "k25", d(2025, 11, 1), 400),
            fact(x.id, "revenue", "Revenues", d(2025, 10, 1), d(2026, 9, 30), "10-K", "k26", d(2026, 11, 1), 480),
            fact(x.id, "revenue", "Revenues", d(2025, 10, 1), d(2026, 6, 30), "10-Q", "q3", d(2026, 8, 1), 360, "Q3"),
            fact(x.id, "revenue", "Revenues", d(2024, 10, 1), d(2025, 6, 30), "10-Q", "q3", d(2026, 8, 1), 300, "Q3"),
        ])
        s.commit()
        assert snapshot(s, x)["ttm"] is None  # 以前は 480＋360−300＝540（年度の値 480 より過大）
        # 10-K の提出前の時点では、その10-Qで直近4四半期を計算できる
        assert snapshot(s, x, as_of=d(2026, 9, 1))["ttm"]["values"]["revenue"] == 400 + 360 - 300


def test_ttm_basis_is_deterministic_and_uses_revenue_period(client):
    """C：説明文は、項目の処理の順序（集合の順序）によらず、売上高の期間で作る。"""
    d = date
    with SessionLocal() as s:
        x = _co(s, "TTMC")
        s.add_all([
            fact(x.id, "revenue", "Revenues", d(2025, 1, 1), d(2025, 12, 31), "10-K", "k", d(2026, 2, 1), 100),
            fact(x.id, "revenue", "Revenues", d(2026, 1, 1), d(2026, 6, 30), "10-Q", "q", d(2026, 8, 1), 60, "Q2"),
            fact(x.id, "revenue", "Revenues", d(2025, 1, 1), d(2025, 6, 30), "10-Q", "q", d(2026, 8, 1), 50, "Q2"),
            fact(x.id, "cfo", "C", d(2025, 1, 1), d(2025, 12, 31), "10-K", "k", d(2026, 2, 1), 30),
            # 営業CFだけ期間が違う（期首が1日ずれた書類の想定）。説明文がこの期間になってはいけない
            fact(x.id, "cfo", "C", d(2026, 1, 2), d(2026, 6, 29), "10-Q", "q", d(2026, 8, 1), 9, "Q2"),
            fact(x.id, "cfo", "C", d(2025, 1, 2), d(2025, 6, 29), "10-Q", "q", d(2026, 8, 1), 7, "Q2"),
        ])
        s.commit()
        t = snapshot(s, x)["ttm"]
    assert t["basis"] == "2025-12-31年度 ＋ 2026-01-01〜2026-06-30 − 前年同期"
