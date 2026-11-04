"""SEC EDGAR（米国証券取引委員会）からの財務データ取得。

SECのアクセス規則（https://www.sec.gov/os/accessing-edgar-data）：
- User-Agent にアプリ名と連絡先（メールアドレス）を含める → 設定 SEC_USER_AGENT
- 毎秒10リクエスト以下 → 本実装は毎秒約8件に制限
"""
import re
import xml.etree.ElementTree as ET

from .http import RateLimitedClient, check

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn_nodash}/index.json"
FILING_FILE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn_nodash}/{name}"

# アプリ内の共通名 → XBRLタグの候補（先頭優先。企業・年度でタグが変わることへの対策：第2・6・9段階の教訓）
CONCEPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "revenue": (("us-gaap", "Revenues"), ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("us-gaap", "SalesRevenueNet"), ("us-gaap", "RevenuesNetOfInterestExpense"),
                # 売上に含めて回収する税（消費税など）を含む売上。他の売上タグがない期間だけ使う（financials._pick）
                ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax")),
    "gross_profit": (("us-gaap", "GrossProfit"),),
    "operating_income": (("us-gaap", "OperatingIncomeLoss"),),
    "pretax_income": (("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"),),
    "income_tax": (("us-gaap", "IncomeTaxExpenseBenefit"),),
    # NetIncomeLoss（親会社株主に帰属）がない企業（例 MA・CAT）は、連結の純利益 ProfitLoss（非支配株主分を含む）を使う
    "net_income": (("us-gaap", "NetIncomeLoss"), ("us-gaap", "ProfitLoss")),
    "net_income_common": (("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),),
    "eps_diluted": (("us-gaap", "EarningsPerShareDiluted"),),
    "shares_diluted": (("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),),
    "shares_outstanding": (("dei", "EntityCommonStockSharesOutstanding"),),
    # 営業CFの合計がない企業（例 APD）は、継続事業の営業CFを使う
    "cfo": (("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
            ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")),
    "capex": (("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"), ("us-gaap", "PaymentsToAcquireProductiveAssets")),
    "dna": (("us-gaap", "DepreciationDepletionAndAmortization"), ("us-gaap", "DepreciationAmortizationAndOther"),
            ("us-gaap", "DepreciationAndAmortization")),
    "dividends_paid": (("us-gaap", "PaymentsOfDividendsCommonStock"), ("us-gaap", "PaymentsOfDividends")),
    "buybacks": (("us-gaap", "PaymentsForRepurchaseOfCommonStock"),),
    "interest_expense": (("us-gaap", "InterestExpense"), ("us-gaap", "InterestExpenseNonoperating")),
    "assets": (("us-gaap", "Assets"),),
    "liabilities": (("us-gaap", "Liabilities"),),
    # StockholdersEquity（親会社株主分）がない企業（例 PG・V）は、非支配株主持分を含む純資産を使う
    "equity": (("us-gaap", "StockholdersEquity"), ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")),
    "cash": (("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),),
    "goodwill": (("us-gaap", "Goodwill"),),
    "debt_noncurrent": (("us-gaap", "LongTermDebtNoncurrent"), ("us-gaap", "LongTermDebtAndCapitalLeaseObligations")),
    "debt_current": (("us-gaap", "DebtCurrent"), ("us-gaap", "LongTermDebtCurrent"),
                     ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsCurrent")),
    "short_term_borrowings": (("us-gaap", "ShortTermBorrowings"), ("us-gaap", "CommercialPaper"), ("us-gaap", "NotesAndLoansPayable")),
    "inventory": (("us-gaap", "InventoryNet"),),
    "dividend_per_share": (("us-gaap", "CommonStockDividendsPerShareDeclared"),),
}
# 単位：通貨（ISO 4217の3文字。USD・CAD・EUR など）、1株あたり（例 USD/shares・CAD/shares）、株数。
# 以前は USD だけを残していたため、カナダドルで報告する企業（例 ENB）の財務データがすべて捨てられていた
_UNIT = re.compile(r"^(?:[A-Z]{3}(?:/shares)?|shares)$")


def unit_ok(unit: str) -> bool:
    return bool(_UNIT.match(unit or ""))


def currency_of(unit: str) -> str | None:
    """金額の単位から通貨を返す（株数は None）。"""
    return unit.split("/")[0] if unit_ok(unit) and unit != "shares" else None


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

    def submissions(self, cik: int) -> dict | None:
        r = self.http.get(SUBMISSIONS_URL.format(cik=cik))
        if r.status_code == 404:
            return None
        check(r)
        return r.json()

    def filing_instance(self, cik: int, accn: str) -> bytes | None:
        """提出書類のXBRLインスタンス（数値データ本体）を取得する。インラインXBRLは SEC が作る *_htm.xml を使う。"""
        nodash = accn.replace("-", "")
        r = self.http.get(FILING_INDEX_URL.format(cik=cik, accn_nodash=nodash))
        if r.status_code == 404:
            return None
        check(r)
        name = instance_name([i["name"] for i in r.json().get("directory", {}).get("item", [])])
        if not name:
            return None
        r = self.http.get(FILING_FILE_URL.format(cik=cik, accn_nodash=nodash, name=name))
        check(r)
        return r.content

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
                if not unit_ok(unit):
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


def periodic_filings(submissions: dict) -> list[dict]:
    """submissions（提出書類の一覧）から、XBRLつきの決算書類（10-K・10-Q など）を新しい順に返す。"""
    r = (submissions or {}).get("filings", {}).get("recent", {})
    n = len(r.get("accessionNumber", []))
    out = []
    for i in range(n):
        if r["form"][i] not in FORMS:
            continue
        if not (r.get("isXBRL", [1] * n)[i] or r.get("isInlineXBRL", [0] * n)[i]):
            continue
        out.append({"accn": r["accessionNumber"][i], "form": r["form"][i], "filed": r["filingDate"][i],
                    "report_date": r.get("reportDate", [None] * n)[i]})
    return sorted(out, key=lambda x: x["filed"], reverse=True)


_SKIP_XML = ("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml", "FilingSummary.xml")


def instance_name(names: list[str]) -> str | None:
    """提出書類のファイル一覧から、XBRLインスタンスのファイル名を選ぶ。"""
    htm = [n for n in names if n.endswith("_htm.xml")]
    if htm:
        return htm[0]
    xml = [n for n in names if n.endswith(".xml") and not n.endswith(_SKIP_XML)]
    return xml[0] if len(xml) == 1 else None


_NS_TAXONOMY = (("http://fasb.org/us-gaap/", "us-gaap"), ("http://xbrl.sec.gov/dei/", "dei"))
_XBRLI = "{http://www.xbrl.org/2003/instance}"


def _local(tag: str) -> tuple[str | None, str]:
    ns, _, name = tag[1:].partition("}") if tag.startswith("{") else ("", "", tag)
    tax = next((t for prefix, t in _NS_TAXONOMY if ns.startswith(prefix)), None)
    return tax, name


def _measure(el) -> str:
    return (el.text or "").strip().split(":")[-1]


def parse_instance(xml: bytes, accn: str, form: str, filed: str) -> dict:
    """XBRLインスタンスを companyfacts と同じ形（facts → taxonomy → tag → units → [値]）に変換する。

    SEC の companyfacts API に、まだ反映されていない提出書類を補うために使う。
    区分（セグメント・ディメンション）つきの値は、会社全体の値ではないため除く（companyfacts と同じ扱い）。
    """
    root = ET.fromstring(xml)
    contexts = {}
    for c in root.iter(f"{_XBRLI}context"):
        if c.find(f"{_XBRLI}entity/{_XBRLI}segment") is not None or c.find(f"{_XBRLI}scenario") is not None:
            continue
        p = c.find(f"{_XBRLI}period")
        inst = p.find(f"{_XBRLI}instant")
        if inst is not None:
            contexts[c.get("id")] = (None, inst.text.strip())
        else:
            contexts[c.get("id")] = (p.find(f"{_XBRLI}startDate").text.strip(), p.find(f"{_XBRLI}endDate").text.strip())
    units = {}
    for u in root.iter(f"{_XBRLI}unit"):
        div = u.find(f"{_XBRLI}divide")
        if div is not None:
            num = div.find(f"{_XBRLI}unitNumerator/{_XBRLI}measure")
            den = div.find(f"{_XBRLI}unitDenominator/{_XBRLI}measure")
            units[u.get("id")] = f"{_measure(num)}/{_measure(den)}"
        else:
            units[u.get("id")] = _measure(u.find(f"{_XBRLI}measure"))
    fy = fp = None
    for el in root:
        tax, name = _local(el.tag)
        if tax == "dei" and name == "DocumentFiscalYearFocus" and el.text:
            fy = int(el.text.strip()[:4])
        elif tax == "dei" and name == "DocumentFiscalPeriodFocus" and el.text:
            fp = el.text.strip()
    facts: dict = {}
    seen = set()
    for el in root:
        tax, name = _local(el.tag)
        ctx, unit = el.get("contextRef"), el.get("unitRef")
        if not tax or ctx not in contexts or unit not in units or el.text is None:
            continue
        if el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
            continue
        try:
            val = float(el.text.strip())
        except ValueError:
            continue
        start, end = contexts[ctx]
        key = (tax, name, units[unit], start, end)
        if key in seen:
            continue
        seen.add(key)
        v = {"end": end, "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": filed}
        if start:
            v["start"] = start
        facts.setdefault(tax, {}).setdefault(name, {"units": {}})["units"].setdefault(units[unit], []).append(v)
    return {"facts": facts}
