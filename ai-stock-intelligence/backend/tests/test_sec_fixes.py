"""SEC財務データの検証（2026-11-04）で見つかった問題の回帰テスト。値はテスト用。"""
from datetime import date, datetime, timezone

from app.db import SessionLocal
from app.models import FinancialFact, Security
from app.providers import sec
from app.services.financials import snapshot
from app.services.ingest_sec import float_anomaly, missing_filings


def _sec(s, code, cik):
    x = Security(market="US", code=code, cik=cik, name=code, name_search=code.lower(), security_type="common",
                 is_active=True, source="test", fetched_at=datetime.now(timezone.utc))
    s.add(x)
    s.flush()
    return x


def _f(sid, concept, tag, start, end, value, unit="USD", form="10-K", accn="k25", filed=date(2026, 2, 20)):
    return FinancialFact(security_id=sid, concept=concept, tag=tag, unit=unit, period_start=start, period_end=end,
                         fiscal_year=end.year, fiscal_period="FY", form=form, accn=accn, filed=filed, value=value,
                         source="test", fetched_at=datetime.now(timezone.utc))


def test_units_accept_any_currency():
    assert all(sec.unit_ok(u) for u in ("USD", "CAD", "EUR", "CAD/shares", "shares"))
    assert not any(sec.unit_ok(u) for u in ("pure", "USD/MWh", "Y", ""))
    assert sec.currency_of("CAD/shares") == "CAD" and sec.currency_of("shares") is None
    cf = {"facts": {"us-gaap": {"Assets": {"units": {"CAD": [
        {"end": "2025-12-31", "val": 5, "accn": "a", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-13"}]}}}}}
    assert sec.extract_facts(cf)[0]["unit"] == "CAD"  # 以前はUSD以外をすべて捨てていた


def test_canadian_dollar_company_does_not_crash_and_reports_currency(client):
    """ENB：カナダドルの値だけの企業。以前は貸借対照表の計算で max() が空になり HTTP 500 だった。"""
    d = date
    with SessionLocal() as s:
        x = _sec(s, "ENB", 895728)
        s.add_all([
            _f(x.id, "revenue", "Revenues", d(2025, 1, 1), d(2025, 12, 31), 60e9, unit="CAD"),
            _f(x.id, "net_income", "NetIncomeLoss", d(2025, 1, 1), d(2025, 12, 31), 6e9, unit="CAD"),
            _f(x.id, "assets", "Assets", None, d(2025, 12, 31), 200e9, unit="CAD"),
            _f(x.id, "eps_diluted", "EarningsPerShareDiluted", d(2025, 1, 1), d(2025, 12, 31), 2.5, unit="CAD/shares"),
            _f(x.id, "shares_outstanding", "EntityCommonStockSharesOutstanding", None, d(2026, 2, 1), 2.2e9, unit="shares"),
            # 別の通貨の値は混ぜない
            _f(x.id, "revenue", "Revenues", d(2025, 1, 1), d(2025, 12, 31), 999e9, unit="USD", accn="other",
               filed=d(2025, 3, 1)),
        ])
        s.commit()
        snap = snapshot(s, x)
        assert snap["currency"] == "CAD" and snap["other_currencies"] == ["USD"]
        assert snap["annual"][-1]["values"]["revenue"] == 60e9
        assert snap["balance"]["values"]["assets"] == 200e9
    r = client.get("/api/v1/securities/US/ENB/financials")
    assert r.status_code == 200 and r.json()["currency"] == "CAD"


def test_only_shares_no_balance_amounts_returns_none_instead_of_crash(client):
    with SessionLocal() as s:
        x = _sec(s, "SHR", 2)
        s.add(_f(x.id, "shares_outstanding", "EntityCommonStockSharesOutstanding", None, date(2026, 2, 1), 1e9, unit="shares"))
        s.commit()
    r = client.get("/api/v1/securities/US/SHR/financials")
    assert r.status_code == 200 and r.json()["balance"] is None


def test_alternative_tags_used_only_when_primary_missing(client):
    d = date
    with SessionLocal() as s:
        a = _sec(s, "ALT", 3)
        s.add_all([
            # NetIncomeLoss がない → ProfitLoss（非支配株主分を含む連結純利益）
            _f(a.id, "net_income", "ProfitLoss", d(2025, 1, 1), d(2025, 12, 31), 10),
            # 税を含む売上だけ → それを使う
            _f(a.id, "revenue", "RevenueFromContractWithCustomerIncludingAssessedTax", d(2025, 1, 1), d(2025, 12, 31), 100),
            _f(a.id, "equity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", None, d(2025, 12, 31), 40),
            _f(a.id, "assets", "Assets", None, d(2025, 12, 31), 90),
        ])
        b = _sec(s, "BOTH", 4)
        s.add_all([
            _f(b.id, "net_income", "NetIncomeLoss", d(2025, 1, 1), d(2025, 12, 31), 8),
            _f(b.id, "net_income", "ProfitLoss", d(2025, 1, 1), d(2025, 12, 31), 10),
            # 税抜きと税込みの両方がある → 税込み（大きい値）ではなく税抜きを使う
            _f(b.id, "revenue", "RevenueFromContractWithCustomerExcludingAssessedTax", d(2025, 1, 1), d(2025, 12, 31), 100),
            _f(b.id, "revenue", "RevenueFromContractWithCustomerIncludingAssessedTax", d(2025, 1, 1), d(2025, 12, 31), 110),
            _f(b.id, "assets", "Assets", None, d(2025, 12, 31), 90),
        ])
        s.commit()
        sa, sb = snapshot(s, a), snapshot(s, b)
        assert sa["annual"][-1]["values"]["net_income"] == 10 and sa["annual"][-1]["values"]["revenue"] == 100
        assert sa["balance"]["values"]["equity"] == 40
        assert sb["annual"][-1]["values"]["net_income"] == 8 and sb["annual"][-1]["values"]["revenue"] == 100
        c = _sec(s, "COMMON", 5)  # BKNG 型：純利益は「普通株主に帰属する純利益」だけ
        s.add_all([_f(c.id, "net_income_common", "NetIncomeLossAvailableToCommonStockholdersBasic", d(2025, 1, 1), d(2025, 12, 31), 7),
                   _f(c.id, "assets", "Assets", None, d(2025, 12, 31), 90)])
        s.commit()
        assert snapshot(s, c)["annual"][-1]["values"]["net_income"] == 7
    assert [n["concept"] for n in client.get("/api/v1/securities/US/COMMON/financials").json()["tag_notes"]] == ["net_income"]
    notes = {n["concept"] for n in client.get("/api/v1/securities/US/ALT/financials").json()["tag_notes"]}
    assert notes == {"net_income", "revenue", "equity"}
    # 報告されていない項目（負債）は推測で埋めない
    assert "liabilities" not in client.get("/api/v1/securities/US/ALT/financials").json()["balance"]["values"]


def test_public_float_scale_errors_are_rejected():
    """実データの例（2026-11）。誤り：Cabot・TTM・Civista・HIVE。正常：AAPL・AZO・PLTR（総資産の22倍）・NVR。"""
    assert float_anomaly(4_429_047.3e9, 52e6, 4e9)  # Cabot：1株あたり約8,600万ドル
    assert float_anomaly(4_165.9e9, 105e6, 5.6e9)  # TTM
    assert float_anomaly(351.1e9, 20.8e6, 4.3e9)  # Civista：1株あたり約1.7万ドル
    assert float_anomaly(950.7e9, 274e6, 0.92e9)  # HIVE：株価では見分けられないが、総資産の約1,000倍
    assert float_anomaly(3_647.1e9, 14_532e6, 404e9) is None  # AAPL
    assert float_anomaly(62.2e9, 16.1e6, 21e9) is None  # AZO：1株あたり約3,900ドル
    assert float_anomaly(299.3e9, None, 13.4e9) is None  # PLTR
    assert float_anomaly(20.3e9, 2.67e6, 5.5e9) is None  # NVR：1株あたり約7,600ドル（実際に高株価）でも総資産の3.7倍で正常
    assert float_anomaly(187.4e9, 31.67e6, 29.26e9) is None  # BKNG：分割前の株数では1株あたり約5,900ドル・総資産の6.4倍
    assert float_anomaly(961.6e9, 58.1e6, 19.2e9)  # Trustmark：総資産の50.1倍・1株あたり約1.7万ドル


def test_missing_filings_and_instance_parser():
    subs = {"filings": {"recent": {
        "accessionNumber": ["n-q3", "n-q2", "n-q1", "n-8k"], "form": ["10-Q", "10-Q", "10-Q", "8-K"],
        "filingDate": ["2026-10-21", "2026-07-24", "2026-04-23", "2026-10-01"], "reportDate": ["2026-09-30", "2026-06-30", "2026-03-31", ""],
        "isXBRL": [1, 1, 1, 0], "isInlineXBRL": [1, 1, 1, 0]}}}
    cf = {"facts": {"us-gaap": {"Assets": {"units": {"USD": [{"end": "2026-03-31", "val": 1, "accn": "n-q1"}]}}}}}
    assert [f["accn"] for f in missing_filings(cf, subs, today=date(2026, 11, 4))] == ["n-q3", "n-q2"]
    assert sec.instance_name(["a.htm", "abt-20260630_cal.xml", "abt-20260630_htm.xml", "FilingSummary.xml"]) == "abt-20260630_htm.xml"
    xml = b"""<?xml version="1.0"?>
<xbrl xmlns="http://www.xbrl.org/2003/instance" xmlns:us-gaap="http://fasb.org/us-gaap/2026" xmlns:dei="http://xbrl.sec.gov/dei/2026"
      xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
  <context id="i"><entity><identifier scheme="x">1</identifier></entity><period><instant>2026-06-30</instant></period></context>
  <context id="d"><entity><identifier scheme="x">1</identifier></entity><period><startDate>2026-01-01</startDate><endDate>2026-06-30</endDate></period></context>
  <context id="seg"><entity><identifier scheme="x">1</identifier><segment><xbrldi:explicitMember dimension="a">b</xbrldi:explicitMember></segment></entity><period><instant>2026-06-30</instant></period></context>
  <unit id="usd"><measure>iso4217:USD</measure></unit>
  <unit id="eps"><divide><unitNumerator><measure>iso4217:USD</measure></unitNumerator><unitDenominator><measure>shares</measure></unitDenominator></divide></unit>
  <dei:DocumentFiscalYearFocus contextRef="d">2026</dei:DocumentFiscalYearFocus>
  <dei:DocumentFiscalPeriodFocus contextRef="d">Q2</dei:DocumentFiscalPeriodFocus>
  <us-gaap:Assets contextRef="i" unitRef="usd" decimals="-6">109215000000</us-gaap:Assets>
  <us-gaap:Assets contextRef="seg" unitRef="usd" decimals="-6">5</us-gaap:Assets>
  <us-gaap:EarningsPerShareDiluted contextRef="d" unitRef="eps" decimals="2">1.50</us-gaap:EarningsPerShareDiluted>
</xbrl>"""
    facts = sec.extract_facts(sec.parse_instance(xml, "n-q2", "10-Q", "2026-07-24"))
    got = {(f["concept"], f["unit"]): f for f in facts}
    assert got[("assets", "USD")]["value"] == 109215000000 and got[("assets", "USD")]["period_start"] is None
    assert got[("eps_diluted", "USD/shares")]["value"] == 1.5 and got[("eps_diluted", "USD/shares")]["fiscal_period"] == "Q2"
    assert len([f for f in facts if f["concept"] == "assets"]) == 1  # 区分つきの値は除く


def test_submissions_failure_still_saves_companyfacts(client):
    """提出書類一覧（submissions）の取得に失敗しても、companyfacts の値は保存する（PR #1 のレビューで見つけた問題）。"""
    from app.models import DataQualityCheck
    from app.services.ingest_sec import ingest_company

    class FakeClient:
        def companyfacts(self, cik):
            return {"facts": {"us-gaap": {"Assets": {"units": {"USD": [
                {"end": "2026-06-30", "val": 7, "accn": "a1", "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-07-30"}]}}}}}

        def submissions(self, cik):
            raise RuntimeError("HTTP 503")

    with SessionLocal() as s:
        x = _sec(s, "SUBF", 9)
        assert ingest_company(s, FakeClient(), x) == 1
        s.commit()
        assert s.query(FinancialFact).filter_by(security_id=x.id).count() == 1
        assert s.query(DataQualityCheck).filter_by(subject="SUBF", check_type="missing").count() == 1
