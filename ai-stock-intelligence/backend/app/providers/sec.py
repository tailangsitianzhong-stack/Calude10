"""SEC EDGAR（米国証券取引委員会）からの財務データ取得。

SECのアクセス規則（https://www.sec.gov/os/accessing-edgar-data）：
- User-Agent にアプリ名と連絡先（メールアドレス）を含める → 設定 SEC_USER_AGENT
- 毎秒10リクエスト以下 → 本実装は毎秒約8件に制限
"""
from .http import RateLimitedClient, check

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"

# アプリ内の共通名 → XBRLタグの候補（先頭優先。企業・年度でタグが変わることへの対策：第2・6・9段階の教訓）
CONCEPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (("us-gaap", "Revenues"), ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("us-gaap", "SalesRevenueNet"), ("us-gaap", "RevenuesNetOfInterestExpense")),
    "gross_profit": (("us-gaap", "GrossProfit"),),
    "operating_income": (("us-gaap", "OperatingIncomeLoss"),),
    "pretax_income": (("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"),),
    "income_tax": (("us-gaap", "IncomeTaxExpenseBenefit"),),
    "net_income": (("us-gaap", "NetIncomeLoss"),),
    "net_income_common": (("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),),
    "eps_diluted": (("us-gaap", "EarningsPerShareDiluted"),),
    "shares_diluted": (("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),),
    "shares_outstanding": (("dei", "EntityCommonStockSharesOutstanding"),),
    "cfo": (("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),),
    "capex": (("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"), ("us-gaap", "PaymentsToAcquireProductiveAssets")),
    "dna": (("us-gaap", "DepreciationDepletionAndAmortization"), ("us-gaap", "DepreciationAmortizationAndOther"),
            ("us-gaap", "DepreciationAndAmortization")),
    "dividends_paid": (("us-gaap", "PaymentsOfDividendsCommonStock"), ("us-gaap", "PaymentsOfDividends")),
    "buybacks": (("us-gaap", "PaymentsForRepurchaseOfCommonStock"),),
    "interest_expense": (("us-gaap", "InterestExpense"), ("us-gaap", "InterestExpenseNonoperating")),
    "assets": (("us-gaap", "Assets"),),
    "liabilities": (("us-gaap", "Liabilities"),),
    "equity": (("us-gaap", "StockholdersEquity"),),
    "cash": (("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),),
    "goodwill": (("us-gaap", "Goodwill"),),
    "debt_noncurrent": (("us-gaap", "LongTermDebtNoncurrent"), ("us-gaap", "LongTermDebtAndCapitalLeaseObligations")),
    "debt_current": (("us-gaap", "DebtCurrent"), ("us-gaap", "LongTermDebtCurrent"),
                     ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsCurrent")),
    "short_term_borrowings": (("us-gaap", "ShortTermBorrowings"), ("us-gaap", "CommercialPaper"), ("us-gaap", "NotesAndLoansPayable")),
    "inventory": (("us-gaap", "InventoryNet"),),
    "dividend_per_share": (("us-gaap", "CommonStockDividendsPerShareDeclared"),),
}
UNITS = ("USD", "USD/shares", "shares")
FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F")


class SecClient:
    def __init__(self, user_agent: str):
        if not user_agent.strip():
            raise ValueError("SEC_USER_AGENT が未設定のため SEC には接続しない")
        self.http = RateLimitedClient(min_interval=0.125, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"})

    def tickers(self) -> dict:
        r = self.http.get(TICKERS_URL)
        check(r)
        return r.json()

    def companyfacts(self, cik: int) -> dict | None:
        r = self.http.get(FACTS_URL.format(cik=cik))
        if r.status_code == 404:
            return None  # XBRLの財務データがない企業（新規上場・外国企業など）
        check(r)
        return r.json()

    def frame(self, taxonomy: str, tag: str, unit: str, period: str) -> dict | None:
        r = self.http.get(FRAME_URL.format(taxonomy=taxonomy, tag=tag, unit=unit, period=period))
        if r.status_code == 404:
            return None
        check(r)
        return r.json()

    def close(self):
        self.http.close()


def extract_facts(companyfacts: dict) -> list[dict]:
    """companyfacts から、対象のタグ・単位・様式の事実だけを取り出す（同じ値の重複は除く）。"""
    out, seen = [], set()
    facts = companyfacts.get("facts", {})
    for concept, candidates in CONCEPTS.items():
        for taxonomy, tag in candidates:
            units = facts.get(taxonomy, {}).get(tag, {}).get("units", {})
            for unit, vals in units.items():
                if unit not in UNITS:
                    continue
                for v in vals:
                    if v.get("form") not in FORMS or "filed" not in v or "end" not in v:
                        continue
                    key = (tag, unit, v.get("start"), v["end"], v["accn"])
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"concept": concept, "tag": tag, "unit": unit, "period_start": v.get("start"),
                                "period_end": v["end"], "fiscal_year": v.get("fy"), "fiscal_period": v.get("fp"),
                                "form": v["form"], "accn": v["accn"], "filed": v["filed"], "value": float(v["val"])})
    return out
