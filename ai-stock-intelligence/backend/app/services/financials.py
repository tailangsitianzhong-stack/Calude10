"""保存済みの財務データ（時点つき）から、年次・直近4四半期（TTM）・最新の貸借対照表を組み立てる。

as_of を指定すると、その日までにSECに提出された書類の値だけを使う（バックテストでの先読みを防ぐ）。
"""
from collections import defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FinancialFact, Security
from ..providers.sec import CONCEPTS

FLOW = {"revenue", "gross_profit", "operating_income", "pretax_income", "income_tax", "net_income", "net_income_common",
        "eps_diluted", "shares_diluted", "cfo", "capex", "dna", "dividends_paid", "buybacks", "interest_expense"}
STOCK = {"assets", "liabilities", "equity", "cash", "goodwill", "debt_noncurrent", "debt_current", "short_term_borrowings",
         "inventory", "shares_outstanding"}
TAG_RANK = {c: {tag: i for i, (_, tag) in enumerate(tags)} for c, tags in CONCEPTS.items()}


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
        return max(cands, key=lambda f: (f.value, f.filed))
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
        out.append(row)
    return out


def ttm(facts: list[FinancialFact]) -> dict | None:
    """直近4四半期 ＝ 直近の年度 ＋ 当年の累計（10-Q）− 前年同期の累計。最新の10-Qの書類の中で当年と前年を対にする。"""
    tenq = [f for f in facts if f.form.startswith("10-Q")]
    if not tenq:
        return None
    latest = max(tenq, key=lambda f: (f.filed, f.period_end))
    accn = latest.accn
    fy = annual(facts, years=1)
    if not fy:
        return None
    fy_vals = fy[-1]["values"]
    out, basis = {}, None
    for concept in FLOW:
        ytd = [f for f in tenq if f.accn == accn and f.concept == concept and (_days(f) or 0) > 100]
        if len(ytd) < 2 or concept not in fy_vals:
            continue
        cur = max(ytd, key=lambda f: f.period_end)
        prev = [f for f in ytd if f.period_end < cur.period_end and abs(_days(f) - _days(cur)) < 10]
        if not prev:
            continue
        p = max(prev, key=lambda f: f.period_end)
        if concept == "revenue":  # 当年と前年で同じタグを使う
            same = [f for f in prev if f.tag == cur.tag]
            p = max(same, key=lambda f: f.period_end) if same else p
        out[concept] = fy_vals[concept] + cur.value - p.value
        basis = f"{fy[-1]['period_end']}年度 ＋ {cur.period_start}〜{cur.period_end} − 前年同期"
    if not out:
        return None
    return {"values": out, "basis": basis, "filed": latest.filed.isoformat(), "period_end": latest.period_end.isoformat()}


def latest_balance(facts: list[FinancialFact]) -> dict | None:
    inst = [f for f in facts if f.concept in STOCK and f.period_start is None]
    if not inst:
        return None
    end = max(f.period_end for f in inst if f.concept in ("assets", "equity", "cash"))
    vals, filed = {}, None
    for concept in STOCK:
        c = [f for f in inst if f.concept == concept and f.period_end == end]
        if concept == "shares_outstanding":  # 表紙の株数は期末日と異なる日付のため、最新のものを使う
            c = sorted([f for f in inst if f.concept == concept], key=lambda f: f.period_end)[-1:]
        if c:
            f = _pick(c, concept)
            vals[concept] = f.value
            filed = max(filed or f.filed.isoformat(), f.filed.isoformat())
    return {"period_end": end.isoformat(), "values": vals, "filed": filed}


def snapshot(session: Session, s: Security, as_of: date | None = None) -> dict:
    facts = load_facts(session, security_ids_for(session, s), as_of)
    if not facts:
        return {"data_status": "not_connected" if s.market == "JP" else "insufficient", "annual": [], "ttm": None,
                "balance": None, "latest_filed": None}
    latest_filed = max(f.filed for f in facts)
    return {"data_status": "stale" if (as_of or date.today()) .toordinal() - latest_filed.toordinal() > 120 else "ok",
            "annual": annual(facts), "ttm": ttm(facts), "balance": latest_balance(facts),
            "latest_filed": latest_filed.isoformat()}
