"""保存済みの財務データ（時点つき）から、年次・直近4四半期（TTM）・最新の貸借対照表を組み立てる。

as_of を指定すると、その日までにSECに提出された書類の値だけを使う（バックテストでの先読みを防ぐ）。
"""
from collections import Counter, defaultdict
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import EdinetDocument, FinancialFact, JpDisclosure, Security
from ..providers.sec import CONCEPTS, currency_of

FLOW = {"revenue", "gross_profit", "operating_income", "pretax_income", "income_tax", "net_income", "net_income_common",
        "eps_diluted", "shares_diluted", "cfo", "capex", "dna", "dividends_paid", "buybacks", "interest_expense",
        # 日本企業（J-Quants の決算短信）だけにある項目。米国企業にはこの共通名の値がないため、米国の結果は変わらない
        "ordinary_income", "eps_basic", "cfi", "cff", "shares_avg"}
STOCK = {"assets", "liabilities", "equity", "cash", "goodwill", "debt_noncurrent", "debt_current", "short_term_borrowings",
         "inventory", "shares_outstanding",
         "net_assets", "equity_ratio", "bps", "shares_issued", "treasury_shares"}
# 年次報告とみなす書類（米国：10-K・20-F・40-F、日本：通期の決算短信）
ANNUAL_FORMS = ("10-K", "20-F", "40-F", "TANSHIN-FY", "EDINET-ASR")
JP_INTERIM_FORMS = ("TANSHIN-Q",)
JP_TAG_PREFIX = ("jquants:", "edinet:")  # 日本企業の値（J-Quants の決算短信、EDINET の有価証券報告書など）
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
    """直近4四半期 ＝ 直近の年度 ＋ 当年の累計（10-Q）− 前年同期の累計。最新の10-Qの書類の中で当年と前年を対にする。

    - 「当年の累計」は、直近の年度末より後に始まり、期間が1年未満の値に限る。10-Q には「過去12か月の合計」
      （例 AMZN の営業CF）が載ることがあり、期末が同じため取り違えると、年度の値に12か月分を足してしまう。
    - 最新の10-Qが直近の10-Kより前の期なら計算しない（年度の値が最新）。古い10-Qの累計を新しい年度に足すと
      二重に数える（例 AAPL・MU：決算期末の後に10-Kだけが出ている時期）。
    - 項目は決まった順で処理し、説明文は売上高（なければ最初に計算できた項目）の期間で作る（実行ごとに変わらない）。
    """
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


# ---- 日本企業（J-Quants の決算短信・EDINET）の組み立て ----
# 規則（2026-11-04 利用者の承認済み）：
# 1. 年度データは、その期の最新の通期の決算短信「1件」の値だけで作る（訂正があれば訂正後の最新）。
#    その書類に記載のない項目は、古い開示から補わず「—」（値なし）にする。
# 2. したがって、会計基準の違う開示の値（例：IFRS の行に日本基準の経常利益）は混ざらない。
# 3. EDINET の負債は、採用した決算短信と同じ会計基準の書類の値だけを使う。
# 4. 決算期変更などで1年ではない期間（350〜380日以外）の通期の決算短信は、年度データとして扱わない
#    （品質の記録 irregular_period に残す：ingest_jquants_fins.record_irregular_periods）。
JP_FULL_YEAR_DAYS = (350, 380)
EDINET_SUPPLEMENT = {"liabilities"}  # 決算短信のサマリーにない項目だけを EDINET から加える


def _jq_disclosures(facts: list[FinancialFact], prefix: str) -> dict[str, list[FinancialFact]]:
    """J-Quants の値を開示番号ごとにまとめる（prefix は form の先頭：TANSHIN-FY・TANSHIN-Q）。"""
    out: dict[str, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.tag.startswith("jquants:") and f.form.startswith(prefix):
            out[f.accn].append(f)
    return out


def _disc_key(fs: list[FinancialFact]) -> tuple:
    return _disclosure_order(fs[0])


def _disc_period(fs: list[FinancialFact]) -> tuple[date | None, date]:
    """開示の対象期間（jp_disclosures の当会計期間）。"""
    f = fs[0]
    return getattr(f, "disc_period_start", None), getattr(f, "disc_period_end", None) or f.period_end


def _is_full_year(fs: list[FinancialFact]) -> bool:
    start, end = _disc_period(fs)
    return start is not None and JP_FULL_YEAR_DAYS[0] <= (end - start).days <= JP_FULL_YEAR_DAYS[1]


def _shown(f: FinancialFact) -> bool:
    return (f.concept in FLOW and f.period_start is not None) or (f.concept in STOCK and f.period_start is None)


def _edinet_for(facts: list[FinancialFact], period_end: date, standard: str | None, concepts: set | None) -> dict:
    """同じ期末・同じ会計基準の EDINET の書類のうち、最新の提出の値（concepts を指定すればその項目だけ）。"""
    docs: dict[str, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        if f.tag.startswith("edinet:") and f.period_end == period_end and getattr(f, "accounting_standard", None) == standard:
            docs[f.accn].append(f)
    if not docs:
        return {}
    latest = max(docs.values(), key=lambda fs: max(x.filed for x in fs))
    return {f.concept: f for f in latest if concepts is None or f.concept in concepts}


def jp_annual(facts: list[FinancialFact], years: int = 6) -> list[dict]:
    by_end: dict[date, list[list[FinancialFact]]] = defaultdict(list)
    for fs in _jq_disclosures(facts, "TANSHIN-FY").values():
        if _is_full_year(fs):  # 決算期変更などの1年でない期間は年度データにしない
            by_end[_disc_period(fs)[1]].append(fs)
    out = []
    for end in sorted(by_end)[-years:]:
        chosen = max(by_end[end], key=_disc_key)  # その期の最新の通期の決算短信1件
        std = getattr(chosen[0], "accounting_standard", None)
        row = {"period_end": end.isoformat(), "values": {}, "filed": chosen[0].filed.isoformat(), "tags": {},
               "accounting_standard": std, "disclosure": chosen[0].accn}
        for f in chosen:
            if _shown(f):
                row["values"][f.concept] = f.value
                row["tags"][f.concept] = f.tag
        for concept, f in _edinet_for(facts, end, std, EDINET_SUPPLEMENT).items():
            row["values"][concept] = f.value
            row["tags"][concept] = f.tag
            row["filed"] = max(row["filed"], f.filed.isoformat())
        if any(c in FLOW for c in row["values"]):
            out.append(row)
    return out


def jp_balance(facts: list[FinancialFact]) -> dict | None:
    """最新の期末の貸借対照表。その期末の最新の決算短信1件の値に、同じ会計基準の EDINET の負債を加える。
    決算短信に貸借対照表の値がない期末は、EDINET の書類の値で示す（取得元の障害時の切り替え）。"""
    jq: dict[date, list[list[FinancialFact]]] = defaultdict(list)
    for prefix in ("TANSHIN-FY", "TANSHIN-Q"):
        for fs in _jq_disclosures(facts, prefix).values():
            inst = [f for f in fs if f.concept in STOCK and f.period_start is None]
            if any(f.concept in ("assets", "net_assets", "cash") for f in inst):
                jq[inst[0].period_end].append(inst)
    ed_ends = {f.period_end for f in facts if f.tag.startswith("edinet:") and f.concept in ("assets", "net_assets", "liabilities")}
    ends = set(jq) | ed_ends
    if not ends:
        return None
    end = max(ends)
    vals, tags, filed = {}, {}, None
    if jq.get(end):
        chosen = max(jq[end], key=_disc_key)
        std = getattr(chosen[0], "accounting_standard", None)
        extra = _edinet_for(facts, end, std, EDINET_SUPPLEMENT)
        picked = list(chosen) + list(extra.values())
    else:
        eds = [f for f in facts if f.tag.startswith("edinet:") and f.period_end == end]
        picked = list(_edinet_for(facts, end, getattr(eds[0], "accounting_standard", None), None).values())
    for f in picked:
        vals[f.concept] = f.value
        tags[f.concept] = f.tag
        filed = max(filed or f.filed.isoformat(), f.filed.isoformat())
    return {"period_end": end.isoformat(), "values": vals, "filed": filed, "tags": tags}


def jp_ttm(facts: list[FinancialFact]) -> dict | None:
    """直近4四半期 ＝ 年度（最新の通期の決算短信1件）＋ 当年の累計（最新の四半期の決算短信1件）
    − 前年同期の累計（前年の同じ四半期の最新の決算短信1件）。3件の会計基準が同じで、3件すべてに記載のある項目だけ。"""
    fy = jp_annual(facts, years=1)
    q = [fs for fs in _jq_disclosures(facts, "TANSHIN-Q").values() if _disc_period(fs)[0] is not None]
    if not fy or not q:
        return None
    cur = max(q, key=lambda fs: (_disc_period(fs)[1], *_disc_key(fs)))
    c_start, c_end = _disc_period(cur)
    if c_end.isoformat() <= fy[-1]["period_end"]:  # 最新が通期なら、年度の値が直近
        return None
    length = (c_end - c_start).days
    prev_c = [fs for fs in q if 350 <= (c_end - _disc_period(fs)[1]).days <= 380
              and abs((_disc_period(fs)[1] - _disc_period(fs)[0]).days - length) < 10]
    if not prev_c:
        return None
    p_end = max(_disc_period(fs)[1] for fs in prev_c)
    prev = max([fs for fs in prev_c if _disc_period(fs)[1] == p_end], key=_disc_key)
    stds = {fy[-1]["accounting_standard"], getattr(cur[0], "accounting_standard", None), getattr(prev[0], "accounting_standard", None)}
    if len(stds) != 1:  # 会計基準が変わった期をまたいで足し引きしない
        return None
    cv = {f.concept: f.value for f in cur if f.concept in FLOW and f.period_start is not None}
    pv = {f.concept: f.value for f in prev if f.concept in FLOW and f.period_start is not None}
    out = {k: fy[-1]["values"][k] + cv[k] - pv[k] for k in sorted(FLOW) if k in cv and k in pv and k in fy[-1]["values"]}
    if not out:
        return None
    p_start = _disc_period(prev)[0]
    return {"values": out, "filed": cur[0].filed.isoformat(), "period_end": c_end.isoformat(),
            "basis": f"{fy[-1]['period_end']}年度 ＋ {c_start}〜{c_end} − 前年同期（{p_start}〜{p_end}）"}


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
    if jp:  # 開示の順序（開示時刻）・会計基準・対象期間（jp_disclosures）と、EDINET の書類の会計基準を付ける
        info = {d: (t, a, ps, pe) for d, t, a, ps, pe in session.execute(
            select(JpDisclosure.disc_no, JpDisclosure.disc_time, JpDisclosure.accounting_standard,
                   JpDisclosure.period_start, JpDisclosure.period_end)
            .where(JpDisclosure.disc_no.in_({f.accn for f in jp if f.tag.startswith("jquants:")})))}
        ed = dict(session.execute(select(EdinetDocument.doc_id, EdinetDocument.accounting_standard)
                                  .where(EdinetDocument.doc_id.in_({f.accn for f in jp if f.tag.startswith("edinet:")}))).all())
        for f in jp:
            if f.tag.startswith("jquants:"):
                f.disc_time, f.accounting_standard, f.disc_period_start, f.disc_period_end = info.get(f.accn, (None, None, None, None))
            else:
                f.disc_time, f.accounting_standard = None, ed.get(f.accn)
    # 古さの判定：日本株は確認済みのプランの遅延（Free は約12週間）を加える。プラン未確認・設定不備なら推測せず、
    # どのプランでも古い場合だけ「古い」とする（services/jquants_plan.py）
    from . import jquants_plan
    jp_delay = jquants_plan.delay_days(get_settings())
    stale_days = 120 + ((jp_delay if jp_delay is not None else jquants_plan.max_delay_days()) if s.market == "JP" else 0)
    cur = reporting_currency(facts)
    others = sorted({c for f in facts if (c := currency_of(f.unit)) and c != cur})
    facts = in_currency(facts, cur)
    if s.market == "JP":  # 日本企業は専用の組み立て（米国企業の計算には影響しない）
        parts = {"annual": jp_annual(facts), "ttm": jp_ttm(facts), "balance": jp_balance(facts)}
    else:
        parts = {"annual": annual(facts), "ttm": ttm(facts), "balance": latest_balance(facts)}
    return {"data_status": "stale" if (as_of or date.today()) .toordinal() - latest_filed.toordinal() > stale_days else "ok",
            **parts, "latest_filed": latest_filed.isoformat(), "currency": cur, "other_currencies": others}
