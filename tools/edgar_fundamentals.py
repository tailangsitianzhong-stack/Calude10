"""SEC EDGAR companyfacts (XBRL) から年次の財務データを抽出し、主要指標を計算する。

使い方:
    python3 -I tools/edgar_fundamentals.py <companyfacts JSONのディレクトリ> ko pep kdp

JSONは https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json から取得し、
<ticker>.json として保存しておく。値は10-Kに記載された値（各年度の最新提出分）を使う。
"""
import json
import sys
from datetime import date

FLOW = {
    "rev": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "gp": ["GrossProfit"],
    "op": ["OperatingIncomeLoss"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"],
    "tax": ["IncomeTaxExpenseBenefit"],
    "ni": ["NetIncomeLoss"],
    "eps": ["EarningsPerShareDiluted"],
    "dil_sh": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "da": ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndOther", "DepreciationAndAmortization"],
    "dep": ["Depreciation"],
    "amort": ["AmortizationOfIntangibleAssets"],
    "div": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "buyback": ["PaymentsForRepurchaseOfCommonStock"],
    "int": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
    "net_int": ["InterestIncomeExpenseNonoperatingNet"],
}
STOCK = {
    "assets": ["Assets"],
    "equity": ["StockholdersEquity"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    # 有利子負債 = 長期借入金(固定) + 1年内返済長期借入金 + 短期借入金・CP
    # 企業や年度によりタグが変わるため、候補を順に補完する（KOは2024年に固定負債のタグを変更）
    "ltd_nc": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "ltd_cur": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "st_debt": ["NotesAndLoansPayable", "ShortTermBorrowings", "CommercialPaper"],
    "debt_cur": ["DebtCurrent"],
    "goodwill": ["Goodwill"],
}


def days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def series(gaap, tags, flow):
    """年度末日 -> 値。10-Kの年次値のみ、同一期間は最新提出を優先。複数タグは先頭優先で補完。"""
    out = {}
    for tag in tags:
        if tag not in gaap:
            continue
        units = gaap[tag]["units"]
        vals = units.get("USD") or units.get("USD/shares") or units.get("shares") or []
        best = {}
        for v in vals:
            if not v.get("form", "").startswith("10-K"):
                continue
            if flow:
                if "start" not in v or not 350 <= days(v["start"], v["end"]) <= 380:
                    continue
            key = v["end"]
            if key not in best or v["filed"] > best[key]["filed"]:
                best[key] = v
        for k, v in best.items():
            out.setdefault(k, v["val"])
    return out


def load(path):
    gaap = json.load(open(path))["facts"]["us-gaap"]
    data = {k: series(gaap, t, True) for k, t in FLOW.items()}
    data.update({k: series(gaap, t, False) for k, t in STOCK.items()})
    return data


def ratios(d, years=5):
    ends = sorted(d["ni"])[-years:]
    rows = []
    for i, e in enumerate(ends):
        g = lambda k: d[k].get(e)
        prev = sorted(d["ni"])[sorted(d["ni"]).index(e) - 1] if sorted(d["ni"]).index(e) > 0 else None
        avg = lambda k: (g(k) + d[k][prev]) / 2 if prev and d[k].get(prev) is not None and g(k) is not None else g(k)
        rev, op, ni = g("rev"), g("op"), g("ni")
        # DebtCurrent（短期の有利子負債合計）があればそれを使い、なければ内訳を合算
        cur = g("debt_cur") if g("debt_cur") is not None else (g("ltd_cur") or 0) + (g("st_debt") or 0)
        debt = (g("ltd_nc") or 0) + cur
        da = g("da") if g("da") is not None else ((g("dep") or 0) + (g("amort") or 0) or None)
        interest = g("int") if g("int") is not None else (-g("net_int") if g("net_int") else None)
        fcf = g("cfo") - g("capex") if g("cfo") is not None and g("capex") is not None else None
        r = {
            "FY end": e,
            "売上高($B)": rev / 1e9 if rev else None,
            "売上成長率": rev / d["rev"][prev] - 1 if prev and rev and d["rev"].get(prev) else None,
            "粗利率": g("gp") / rev if g("gp") and rev else None,
            "営業利益率": op / rev if op and rev else None,
            "純利益率": ni / rev if rev else None,
            "希薄化EPS(報告)": g("eps"),
            "EPS(計算=NI/希薄化株数)": ni / g("dil_sh") if g("dil_sh") else None,
            "ROE(平均自己資本)": ni / avg("equity") if avg("equity") else None,
            "ROA(平均総資産)": ni / avg("assets") if avg("assets") else None,
            "総資産回転率(売上/平均総資産)": rev / avg("assets") if rev and avg("assets") else None,
            "財務レバレッジ(平均総資産/平均自己資本)": avg("assets") / avg("equity") if avg("assets") and avg("equity") else None,
            "ROIC(税引後営業利益/(有利子負債+自己資本-現金))": op * (1 - g("tax") / g("pretax")) / (debt + g("equity") - (g("cash") or 0)) if op and g("tax") and g("pretax") and g("equity") else None,
            "自己資本比率": g("equity") / g("assets") if g("equity") and g("assets") else None,
            "有利子負債($B)": debt / 1e9,
            "ネット有利子負債/EBITDA": (debt - (g("cash") or 0)) / (op + da) if op and da else None,
            "インタレストカバレッジ(営業利益/支払利息)": op / interest if op and interest else None,
            "FCF($B)": fcf / 1e9 if fcf is not None else None,
            "FCF/純利益": fcf / ni if fcf is not None and ni else None,
            "配当性向(配当支払/純利益)": g("div") / ni if g("div") and ni else None,
            "総還元性向((配当+自社株買い)/純利益)": ((g("div") or 0) + (g("buyback") or 0)) / ni if ni else None,
            "実効税率": g("tax") / g("pretax") if g("tax") and g("pretax") else None,
            "のれん/自己資本": g("goodwill") / g("equity") if g("goodwill") and g("equity") else None,
        }
        rows.append(r)
    return rows


def fmt(v, k):
    if v is None:
        return "—"
    if "$B" in k:
        return f"{v:,.2f}"
    if "EPS" in k:
        return f"{v:.2f}"
    if "回転率" in k:
        return f"{v:.2f}x"
    if "/EBITDA" in k or "カバレッジ" in k or "レバレッジ" in k:
        return f"{v:.1f}x"
    return f"{v*100:.1f}%"


if __name__ == "__main__":
    base, tickers = sys.argv[1], sys.argv[2:]
    for t in tickers:
        rows = ratios(load(f"{base}/{t}.json"))
        keys = list(rows[0].keys())
        print(f"\n### {t.upper()}\n")
        print("| 指標 | " + " | ".join(r["FY end"] for r in rows) + " |")
        print("|---|" + "---|" * len(rows))
        for k in keys[1:]:
            print(f"| {k} | " + " | ".join(fmt(r[k], k) for r in rows) + " |")
