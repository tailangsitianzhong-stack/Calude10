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
