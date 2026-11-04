"""保存済みの財務データ（時点つき）から、年次・直近4四半期（TTM）・最新の貸借対照表を組み立てる。

as_of を指定すると、その日までにSECに提出された書類の値だけを使う（バックテストでの先読みを防ぐ）。
"""
from collections import Counter, defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FinancialFact, Security
from ..providers.sec import CONCEPTS, currency_of

FLOW = {"revenue", "gross_profit", "operating_income", "pretax_income", "income_tax", "net_income", "net_income_common",
        "eps_diluted", "shares_diluted", "cfo", "capex", "dna", "dividends_paid", "buybacks", "interest_expense"}
STOCK = {"assets", "liabilities", "equity", "cash", "goodwill", "debt_noncurrent", "debt_current", "short_term_borrowings",
         "inventory", "shares_outstanding"}
TAG_RANK = {c: {tag: i for i, (_, tag) in enumerate(tags)} for c, tags in CONCEPTS.items()}
# 売上の候補のうち、他の売上タグがない期間だけ使うもの（税を含む売上は、税抜きの売上より大きくなるため）
REVENUE_FALLBACK_TAGS = {"RevenueFromContractWithCustomerIncludingAssessedTax"}


# 項目の報告がない期間だけ、企業が報告した別の項目の値を使う（例 BKNG は2016年以降、純利益を
# 「普通株主に帰属する純利益」だけで報告）。値を推測・計算するのではなく、どの項目を使ったかを tags に残す
FALLBACK_CONCEPTS = {"net_income": "net_income_common"}


def _apply_fallbacks(values: dict, tags: dict | None) -> None:
    for concept, alt in FALLBACK_CONCEPTS.items():
        if concept not in values and alt in values:
            values[concept] = values[alt]
            if tags is not None and alt in tags:
                tags[concept] = tags[alt]


def reporting_currency(facts: list[FinancialFact]) -> str | None:
    """財務データを報告している通貨（例 USD・CAD）。最新の年次報告の金額の単位で決め、なければ最新の書類で決める。"""
    money = [f for f in facts if currency_of(f.unit)]
    if not money:
        return None
    annual_forms = [f for f in money if f.form.startswith(("10-K", "20-F", "40-F"))]
    pool = annual_forms or money
    latest = max(f.filed for f in pool)
    return Counter(currency_of(f.unit) for f in pool if f.filed == latest).most_common(1)[0][0]


def in_currency(facts: list[FinancialFact], cur: str | None) -> list[FinancialFact]:
    """報告通貨の金額・1株あたりの値と、株数だけを残す（通貨の違う値を混ぜて計算しない）。"""
    return [f for f in facts if f.unit == "shares" or currency_of(f.unit) == cur]


def security_ids_for(session: Session, s: Security) -> list[int]:
    """同じ企業（同じCIK）の複数の銘柄クラス（例 GOOG / GOOGL）で財務データを共有する。"""
    if s.cik is None:
        return [s.id]
    return [i for (i,) in session.execute(select(Security.id).where(Security.cik == s.cik))]


def load_facts(session: Session, ids: list[int], as_of: date | None) -> list[FinancialFact]:
    q = select(FinancialFact).where(FinancialFact.security_id.in_(ids))
    if as_of:
        q = q.where(FinancialFact.filed <= as_of)
    return list(session.scalars(q))


def _days(f: FinancialFact) -> int | None:
    return (f.period_end - f.period_start).days if f.period_start else None


def _pick(cands: list[FinancialFact], concept: str) -> FinancialFact:
    """同じ期間の候補から1つ選ぶ。売上は、一部の売上項目だけのタグを拾う誤り（第6段階）を避けるため最大値。
    それ以外はタグの優先順位、同順位なら最新の提出。"""
    if concept == "revenue":
        main = [f for f in cands if f.tag not in REVENUE_FALLBACK_TAGS] or cands
        return max(main, key=lambda f: (f.value, f.filed))
    return min(cands, key=lambda f: (TAG_RANK[concept].get(f.tag, 99), -f.filed.toordinal()))


def annual(facts: list[FinancialFact], years: int = 6) -> list[dict]:
    """10-K（年次報告）の年度の値。期間の長さが350〜380日のものだけを年次とみなす。"""
    by = defaultdict(lambda: defaultdict(list))
    for f in facts:
        if not f.form.startswith(("10-K", "20-F", "40-F")):
            continue
        if f.concept in FLOW and (d := _days(f)) is not None and 350 <= d <= 380:
            by[f.period_end][f.concept].append(f)
        elif f.concept in STOCK and f.period_start is None:
            by[f.period_end][f.concept].append(f)
    # 年度末＝フローの値がある期間末
    ends = sorted(e for e in by if any(c in FLOW for c in by[e]))[-years:]
    out = []
    for e in ends:
        row = {"period_end": e.isoformat(), "values": {}, "filed": None, "tags": {}}
        for concept, cands in by[e].items():
            f = _pick(cands, concept)
            row["values"][concept] = f.value
            row["tags"][concept] = f.tag
            row["filed"] = max(row["filed"] or f.filed.isoformat(), f.filed.isoformat())
        _apply_fallbacks(row["values"], row["tags"])
        out.append(row)
    return out


def ttm(facts: list[FinancialFact]) -> dict | None:
    """直近4四半期 ＝ 直近の年度 ＋ 当年の累計（10-Q）− 前年同期の累計。最新の10-Qの書類の中で当年と前年を対にする。

    - 「当年の累計」は、直近の年度末より後に始まり、期間が1年未満の値に限る。10-Q には「過去12か月の合計」
      （例 AMZN の営業CF）が載ることがあり、期末が同じため取り違えると、年度の値に12か月分を足してしまう。
    - 最新の10-Qが直近の10-Kより前の期なら計算しない（年度の値が最新）。古い10-Qの累計を新しい年度に足すと
      二重に数える（例 AAPL・MU：決算期末の後に10-Kだけが出ている時期）。
    - 項目は決まった順で処理し、説明文は売上高（なければ最初に計算できた項目）の期間で作る（実行ごとに変わらない）。
    """
    tenq = [f for f in facts if f.form.startswith("10-Q")]
    if not tenq:
        return None
    latest = max(tenq, key=lambda f: (f.filed, f.period_end))
    accn = latest.accn
    fy = annual(facts, years=1)
    if not fy:
        return None
    fy_end = date.fromisoformat(fy[-1]["period_end"])
    fy_vals = fy[-1]["values"]
    out, periods = {}, {}
    for concept in sorted(FLOW):
        ytd = [f for f in tenq if f.accn == accn and f.concept == concept and (_days(f) or 0) > 100]
        if len(ytd) < 2 or concept not in fy_vals:
            continue
        cur_c = [f for f in ytd if f.period_start > fy_end and _days(f) < 350]
        if not cur_c:  # 最新の10-Qが直近の年度より前の期、または当年の累計がない
            continue
        cur = max(cur_c, key=lambda f: f.period_end)
        prev = [f for f in ytd if f.period_end < cur.period_end and abs(_days(f) - _days(cur)) < 10]
        if not prev:
            continue
        p = max(prev, key=lambda f: f.period_end)
        if concept == "revenue":  # 当年と前年で同じタグを使う
            same = [f for f in prev if f.tag == cur.tag]
            p = max(same, key=lambda f: f.period_end) if same else p
        out[concept] = fy_vals[concept] + cur.value - p.value
        periods[concept] = (cur.period_start, cur.period_end)
    if not out:
        return None
    start, end = periods.get("revenue") or periods[next(iter(periods))]
    _apply_fallbacks(out, None)
    return {"values": out, "basis": f"{fy[-1]['period_end']}年度 ＋ {start}〜{end} − 前年同期",
            "filed": latest.filed.isoformat(), "period_end": latest.period_end.isoformat()}


def latest_balance(facts: list[FinancialFact]) -> dict | None:
    inst = [f for f in facts if f.concept in STOCK and f.period_start is None]
    if not inst:
        return None
    core = [f.period_end for f in inst if f.concept in ("assets", "equity", "cash")]
    if not core:  # 株数だけで、貸借対照表の金額がない（例：報告通貨の値がない）
        return None
    end = max(core)
    vals, tags, filed = {}, {}, None
    for concept in STOCK:
        c = [f for f in inst if f.concept == concept and f.period_end == end]
        if concept == "shares_outstanding":  # 表紙の株数は期末日と異なる日付のため、最新のものを使う
            c = sorted([f for f in inst if f.concept == concept], key=lambda f: f.period_end)[-1:]
        if c:
            f = _pick(c, concept)
            vals[concept] = f.value
            tags[concept] = f.tag
            filed = max(filed or f.filed.isoformat(), f.filed.isoformat())
    return {"period_end": end.isoformat(), "values": vals, "filed": filed, "tags": tags}


def snapshot(session: Session, s: Security, as_of: date | None = None) -> dict:
    facts = load_facts(session, security_ids_for(session, s), as_of)
    if not facts:
        return {"data_status": "not_connected" if s.market == "JP" else "insufficient", "annual": [], "ttm": None,
                "balance": None, "latest_filed": None, "currency": None, "other_currencies": []}
    latest_filed = max(f.filed for f in facts)
    cur = reporting_currency(facts)
    others = sorted({c for f in facts if (c := currency_of(f.unit)) and c != cur})
    facts = in_currency(facts, cur)
    return {"data_status": "stale" if (as_of or date.today()) .toordinal() - latest_filed.toordinal() > 120 else "ok",
            "annual": annual(facts), "ttm": ttm(facts), "balance": latest_balance(facts),
            "latest_filed": latest_filed.isoformat(), "currency": cur, "other_currencies": others}
