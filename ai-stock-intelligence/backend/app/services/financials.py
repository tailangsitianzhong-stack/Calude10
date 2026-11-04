"""保存済みの財務データ（時点つき）から、年次・直近4四半期（TTM）・最新の貸借対照表を組み立てる。

as_of を指定すると、その日までにSECに提出された書類の値だけを使う（バックテストでの先読みを防ぐ）。
"""
from collections import Counter, defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import FinancialFact, JpDisclosure, Security
from ..providers import jquants
from ..providers.sec import CONCEPTS, currency_of

FLOW = {"revenue", "gross_profit", "operating_income", "pretax_income", "income_tax", "net_income", "net_income_common",
        "eps_diluted", "shares_diluted", "cfo", "capex", "dna", "dividends_paid", "buybacks", "interest_expense",
        # 日本企業（J-Quants の決算短信）だけにある項目。米国企業にはこの共通名の値がないため、米国の結果は変わらない
        "ordinary_income", "eps_basic", "cfi", "cff", "shares_avg"}
STOCK = {"assets", "liabilities", "equity", "cash", "goodwill", "debt_noncurrent", "debt_current", "short_term_borrowings",
         "inventory", "shares_outstanding",
         "net_assets", "equity_ratio", "bps", "shares_issued", "treasury_shares"}
# 年次報告とみなす書類（米国：10-K・20-F・40-F、日本：通期の決算短信）
ANNUAL_FORMS = ("10-K", "20-F", "40-F", "TANSHIN-FY")
JP_INTERIM_FORMS = ("TANSHIN-Q",)
JP_TAG_PREFIX = "jquants:"
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
    annual_forms = [f for f in money if f.form.startswith(ANNUAL_FORMS)]
    pool = annual_forms or money
    latest = max(f.filed for f in pool)
    return Counter(currency_of(f.unit) for f in pool if f.filed == latest).most_common(1)[0][0]


def in_currency(facts: list[FinancialFact], cur: str | None) -> list[FinancialFact]:
    """報告通貨の金額・1株あたりの値と、株数だけを残す（通貨の違う値を混ぜて計算しない）。"""
    return [f for f in facts if f.unit in ("shares", "pure") or currency_of(f.unit) == cur]


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


def _disclosure_order(f: FinancialFact) -> tuple:
    """開示の新しさ。開示番号は開示順を保証しないため、開示日・開示時刻で比べる（J-Quants 公式仕様）。"""
    return (f.filed, getattr(f, "disc_time", None) or "")


def _pick(cands: list[FinancialFact], concept: str) -> FinancialFact:
    """同じ期間の候補から1つ選ぶ。売上は、一部の売上項目だけのタグを拾う誤り（第6段階）を避けるため最大値。
    それ以外はタグの優先順位、同順位なら最新の提出。
    日本企業（J-Quants）は1項目1タグで、同じ期間の候補は訂正前後の開示なので、いちばん新しい開示の値を使う
    （古い決算短信の値で新しい値を置き換えない）。"""
    if all(f.tag.startswith(JP_TAG_PREFIX) for f in cands):
        return max(cands, key=_disclosure_order)
    if concept == "revenue":
        main = [f for f in cands if f.tag not in REVENUE_FALLBACK_TAGS] or cands
        return max(main, key=lambda f: (f.value, f.filed))
    return min(cands, key=lambda f: (TAG_RANK.get(concept, {}).get(f.tag, 99), -f.filed.toordinal()))


def annual(facts: list[FinancialFact], years: int = 6) -> list[dict]:
    """10-K（年次報告）の年度の値。期間の長さが350〜380日のものだけを年次とみなす。"""
    by = defaultdict(lambda: defaultdict(list))
    for f in facts:
        if not f.form.startswith(ANNUAL_FORMS):
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
        stds = {getattr(f, "accounting_standard", None) for cands in by[e].values() for f in cands} - {None}
        if stds:  # 日本企業：その年度の会計基準（決算短信の開示書類種別から）。米国企業には付けない
            row["accounting_standard"] = "・".join(sorted(stds))
        out.append(row)
    return out


def ttm_jp(facts: list[FinancialFact]) -> dict | None:
    """日本企業の直近4四半期 ＝ 直近の年度 ＋ 当年の累計 − 前年同期の累計。

    決算短信のサマリーには前年同期の値がないため、前年の同じ四半期の開示（期間の長さが同じで、期末が約1年前）を使う。
    前年同期の開示がない項目は計算しない（推測で埋めない）。最新の開示が通期なら、年度の値そのものが直近なので計算しない。"""
    interim = [f for f in facts if f.form.startswith(JP_INTERIM_FORMS) and f.concept in FLOW and f.period_start]
    fy = annual(facts, years=1)
    if not interim or not fy:
        return None
    latest = max(interim, key=lambda f: (f.period_end, *_disclosure_order(f)))
    if latest.period_end.isoformat() <= fy[-1]["period_end"]:
        return None
    fy_vals = fy[-1]["values"]
    out, basis = {}, None
    for concept in FLOW:
        cur_c = [f for f in interim if f.concept == concept and f.period_end == latest.period_end]
        if not cur_c or concept not in fy_vals:
            continue
        cur = _pick(cur_c, concept)
        prev_c = [f for f in interim if f.concept == concept and 350 <= (cur.period_end - f.period_end).days <= 380
                  and abs(_days(f) - _days(cur)) < 10]
        if not prev_c:
            continue
        p = _pick([f for f in prev_c if f.period_end == max(x.period_end for x in prev_c)], concept)
        # 会計基準が変わった期（例：日本基準→IFRS）をまたいで足し引きしない
        stds = {getattr(x, "accounting_standard", None) for x in (cur, p)} | {fy[-1].get("accounting_standard")}
        if len(stds) != 1:
            continue
        out[concept] = fy_vals[concept] + cur.value - p.value
        basis = f"{fy[-1]['period_end']}年度 ＋ {cur.period_start}〜{cur.period_end} − 前年同期（{p.period_start}〜{p.period_end}）"
    if not out:
        return None
    return {"values": out, "basis": basis, "filed": latest.filed.isoformat(), "period_end": latest.period_end.isoformat()}


def ttm(facts: list[FinancialFact]) -> dict | None:
    """直近4四半期 ＝ 直近の年度 ＋ 当年の累計（10-Q）− 前年同期の累計。最新の10-Qの書類の中で当年と前年を対にする。"""
    if any(f.form.startswith(JP_INTERIM_FORMS) for f in facts):
        return ttm_jp(facts)
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
    _apply_fallbacks(out, None)
    return {"values": out, "basis": basis, "filed": latest.filed.isoformat(), "period_end": latest.period_end.isoformat()}


def latest_balance(facts: list[FinancialFact]) -> dict | None:
    inst = [f for f in facts if f.concept in STOCK and f.period_start is None]
    if not inst:
        return None
    core = [f.period_end for f in inst if f.concept in ("assets", "equity", "cash", "net_assets")]
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
    jp = [f for f in facts if f.tag.startswith(JP_TAG_PREFIX)]
    if jp:  # 訂正開示の順序の判定に使う開示時刻と、会計基準（jp_disclosures）を付ける
        info = {d: (t, a) for d, t, a in session.execute(
            select(JpDisclosure.disc_no, JpDisclosure.disc_time, JpDisclosure.accounting_standard)
            .where(JpDisclosure.disc_no.in_({f.accn for f in jp})))}
        for f in jp:
            f.disc_time, f.accounting_standard = info.get(f.accn, (None, None))
    # 古さの判定：日本株の Freeプランは約12週間遅れてしか取得できないため、その分を加える
    stale_days = 120 + (jquants.DELAY_DAYS.get(get_settings().jquants_plan, 0) if s.market == "JP" else 0)
    cur = reporting_currency(facts)
    others = sorted({c for f in facts if (c := currency_of(f.unit)) and c != cur})
    facts = in_currency(facts, cur)
    return {"data_status": "stale" if (as_of or date.today()) .toordinal() - latest_filed.toordinal() > stale_days else "ok",
            "annual": annual(facts), "ttm": ttm(facts), "balance": latest_balance(facts),
            "latest_filed": latest_filed.isoformat(), "currency": cur, "other_currencies": others}
