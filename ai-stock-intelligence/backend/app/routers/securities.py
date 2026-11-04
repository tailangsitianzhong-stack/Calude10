from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..config import get_settings
from ..models import DailyPrice, DataQualityCheck, IngestionRun, Security
from ..providers import jquants
from ..services.financials import snapshot
from ..services.quality import staleness
from ..schemas import Quote, SearchResult, Section, SecurityDetail, SecurityOut
from ..services.text import normalize

router = APIRouter(prefix="/api/v1/securities", tags=["securities"])

CURRENCY = {"JP": "JPY", "US": "USD"}


def to_out(s: Security) -> SecurityOut:
    return SecurityOut(market=s.market, code=s.code, cik=s.cik, name=s.name, exchange=s.exchange, sector=s.sector,
                       size_class=s.size_class, security_type=s.security_type, is_active=s.is_active,
                       source=s.source, source_as_of=s.source_as_of)


@router.get("", response_model=SearchResult)
def search(query: str = Query("", max_length=64), market: str | None = Query(None, pattern="^(JP|US)$"),
           include_funds: bool = False, limit: int = Query(30, ge=1, le=100),
           session: Session = Depends(get_session)) -> SearchResult:
    q = normalize(query)
    stmt = select(Security).where(Security.is_active.is_(True))
    if market:
        stmt = stmt.where(Security.market == market)
    if not include_funds:
        stmt = stmt.where(Security.security_type == "common")
    if q:
        code_q = q.upper()
        stmt = stmt.where(or_(func.upper(Security.code) == code_q, func.upper(Security.code).like(code_q + "%"),
                              Security.name_search.like("%" + q + "%")))
        rank = case((func.upper(Security.code) == code_q, 0), (func.upper(Security.code).like(code_q + "%"), 1),
                    (Security.name_search.like(q + "%"), 2), else_=3)
        # 同じ順位の中では、日本株は TOPIX の規模区分の大きい順（流動性の目安）に並べる
        size = case((Security.size_class == "TOPIX Core30", 0), (Security.size_class == "TOPIX Large70", 1),
                    (Security.size_class == "TOPIX Mid400", 2), (Security.size_class.like("TOPIX Small%"), 3), else_=4)
        stmt = stmt.order_by(rank, size, func.length(Security.name), Security.code)
    else:
        stmt = stmt.order_by(Security.market, Security.code)
    total = session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    items = [to_out(s) for s in session.scalars(stmt.limit(limit))]
    return SearchResult(query=query, market=market, total=total, items=items)


def get_security(session: Session, market: str, code: str) -> Security:
    s = session.scalar(select(Security).where(Security.market == market.upper(), Security.code == code.upper()))
    if s is None:
        raise HTTPException(status_code=404, detail="銘柄が見つかりません")
    return s


PRICE_PROVIDER_LABEL = {"jquants": "J-Quants API V2", "tiingo": "Tiingo", "massive": "Massive（旧Polygon.io）"}


# 株価の取得元の障害時に表示する説明（日本株は J-Quants 以外に使える取得元がない）
OUTAGE_NOTE = {"JP": "株価の取得元（J-Quants）の障害で最新の取得に失敗しました。日本株の株価に代わりの取得元はありません",
               "US": "株価の取得元の障害で最新の取得に失敗しました（代わりの取得元でも取得できませんでした）"}


def last_price_run_failed(session: Session, market: str) -> bool:
    run = session.scalar(select(IngestionRun).where(IngestionRun.dataset == ("prices_jp" if market == "JP" else "prices_us"),
                                                    IngestionRun.status != "running")
                         .order_by(IngestionRun.id.desc()).limit(1))
    return bool(run and run.status == "failed")


def latest_quote(session: Session, s: Security) -> Quote:
    p = session.scalar(select(DailyPrice).where(DailyPrice.security_id == s.id).order_by(DailyPrice.trade_date.desc()).limit(1))
    cur = CURRENCY.get(s.market, "")
    failed = last_price_run_failed(session, s.market)
    if p is None:
        if failed:  # 設定はあるが取得元の障害で取得できていない。値は作らない
            return Quote(price=None, currency=cur, as_of=None, provider=None, is_delayed=None, data_status="error",
                         note=OUTAGE_NOTE[s.market])
        why = "J-Quants API キー未設定のため未接続" if s.market == "JP" else "米国株の株価API（Tiingo / Massive）が未設定のため未接続"
        return Quote(price=None, currency=cur, as_of=None, provider=None, is_delayed=None, data_status="not_connected", note=why)
    stale, age = staleness(p.trade_date, "daily")
    if p.provider == "jquants":
        note = jquants.DELAY_NOTE.get(get_settings().jquants_plan, "") + "。リアルタイムの株価ではありません"
    else:
        note = "日足の終値。リアルタイムの株価ではありません"
    if failed:  # 保存済みの終値は基準日つきで示し、取得元の障害であることを明示する（「最新」とは表示しない）
        note = f"{OUTAGE_NOTE[s.market]}。表示は {p.trade_date} の保存済みの終値です。{note}"
        stale = True
    return Quote(price=p.close, currency=cur, as_of=p.trade_date, fetched_at=p.fetched_at,
                 provider=PRICE_PROVIDER_LABEL.get(p.provider, p.provider), is_delayed=True,
                 data_status="stale" if stale else "ok", note=note)


@router.get("/{market}/{code}", response_model=SecurityDetail)
def detail(market: str, code: str, session: Session = Depends(get_session)) -> SecurityDetail:
    s = get_security(session, market, code)
    return SecurityDetail(security=to_out(s), quote=latest_quote(session, s))


@router.get("/{market}/{code}/prices")
def prices(market: str, code: str, days: int = Query(365, ge=5, le=3650), session: Session = Depends(get_session)) -> dict:
    s = get_security(session, market, code)
    rows = list(session.scalars(select(DailyPrice).where(DailyPrice.security_id == s.id).order_by(DailyPrice.trade_date.desc()).limit(days)))
    rows.reverse()
    return {"data_status": "ok" if rows else "not_connected", "provider": rows[-1].provider if rows else None,
            "points": [[r.trade_date.isoformat(), r.close, r.volume] for r in rows]}


LABELS = {"revenue": "売上高", "gross_profit": "売上総利益", "operating_income": "営業利益", "net_income": "純利益",
          "eps_diluted": "希薄化EPS", "cfo": "営業キャッシュフロー", "capex": "設備投資", "dividends_paid": "配当支払",
          "buybacks": "自社株買い", "assets": "総資産", "liabilities": "負債", "equity": "株主資本", "cash": "現金",
          "debt_noncurrent": "長期借入金", "debt_current": "短期の借入金", "shares_outstanding": "発行済株式数"}
# 日本企業（J-Quants の決算短信）の項目。米国企業の応答は変えない（日本株のときだけ加える）
JP_LABELS = {"ordinary_income": "経常利益", "net_income": "純利益（親会社株主に帰属）", "eps_basic": "EPS",
             "eps_diluted": "希薄化EPS", "net_assets": "純資産（非支配株主持分を含む）", "equity_ratio": "自己資本比率",
             "bps": "1株あたり純資産", "cfi": "投資キャッシュフロー", "cff": "財務キャッシュフロー",
             "shares_issued": "期末発行済株式数（自己株式を含む）", "treasury_shares": "期末自己株式数",
             "dividend_per_share": "1株あたり配当（年間）"}
JP_SOURCE = "J-Quants API V2 財務情報（決算短信のサマリー。上場会社が提出したXBRLに基づく）"
JP_ITEM_NOTES = [
    "純資産は J-Quants の「純資産（Eq）」で、非支配株主持分などを含みます（米国企業の「株主資本」とは別の項目）。",
    "経常利益は日本基準の項目です。IFRS・米国基準の企業には概念がないため「—」になります。",
    "負債の合計は、決算短信のサマリーに項目がないため、EDINET の有価証券報告書・半期報告書に報告された値を表示します。"
    "第1・第3四半期や、EDINET から未取得の期は「—」です（推測で計算しません）。",
    "金額は円単位で収録された値で、損益・キャッシュフローは期首からの累計です。",
]


@router.get("/{market}/{code}/financials")
def financials(market: str, code: str, as_of: date | None = Query(None, description="この日までに提出された書類だけを使う"),
               session: Session = Depends(get_session)) -> dict:
    s = get_security(session, market, code)
    snap = snapshot(session, s, as_of)
    if s.market == "JP":
        qs = select(DataQualityCheck).where(DataQualityCheck.dataset == "jquants_fins",
                                            DataQualityCheck.subject.in_((s.code, jquants.to_jq_code(s.code))))
    else:
        qs = select(DataQualityCheck).where(DataQualityCheck.dataset == "sec", DataQualityCheck.subject == s.code)
    issues = [{"type": q.check_type, "detail": q.detail} for q in session.scalars(qs)]
    note = {"JP": "日本株の財務データは未取得です（J-Quants の財務情報を取得した銘柄だけ表示します）",
            "US": "SEC EDGAR の取得対象外の銘柄（対象は公開流通時価総額の大きい順）"}[s.market] if snap["data_status"] in ("not_connected", "insufficient") else None
    out = {"security": to_out(s).model_dump(), "as_of": as_of, "source": "SEC EDGAR XBRL（企業がSECに提出した書類）" if s.market == "US" else None,
           "reliability": "一次情報" if s.market == "US" else None, "labels": LABELS, "note": note, "quality_issues": issues,
           "tag_notes": tag_notes(snap), **snap}
    if s.market == "JP" and snap["annual"]:
        plan = get_settings().jquants_plan
        out.update(source=JP_SOURCE, reliability="公式データ（日本取引所グループ）", labels={**LABELS, **JP_LABELS},
                   item_notes=JP_ITEM_NOTES,
                   delay_note=("Freeプランのため、決算短信は開示から約12週間後にしか取得できません。最新の決算が反映されていない場合があります"
                               if plan == "free" else None))
    return out


# 代わりのXBRLタグを使ったとき、意味の違いを画面で説明する（値を補完したのではなく、企業が報告した別の項目）
TAG_NOTES = {
    "ProfitLoss": "親会社株主に帰属する純利益の報告がないため、非支配株主分を含む連結の純利益",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": "親会社株主分の報告がないため、非支配株主持分を含む純資産",
    "RevenueFromContractWithCustomerIncludingAssessedTax": "税抜きの売上の報告がないため、売上に含めて回収する税を含む売上",
    "NetIncomeLossAvailableToCommonStockholdersBasic": "純利益の報告がないため、普通株主に帰属する純利益",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations": "営業CFの合計の報告がないため、継続事業の営業CF",
}


def tag_notes(snap: dict) -> list[dict]:
    rows = snap["annual"][-1:] + ([snap["balance"]] if snap["balance"] else [])
    out, seen = [], set()
    for r in rows:
        for concept, tag in (r.get("tags") or {}).items():
            if tag in TAG_NOTES and concept not in seen and concept in LABELS:
                seen.add(concept)
                out.append({"concept": concept, "tag": tag, "note": TAG_NOTES[tag]})
    return out


# (表示名, 実装予定のフェーズ, 表示予定の内容)
SECTIONS = {
    "score": ("AI総合評価（100点満点）", 4, "9項目の採点内訳・項目ごとの信頼度・根拠・採点できなかった項目を表示します。"),
    "forecast": ("将来株価予測", 5, "1か月・3か月・6か月・1年の上昇／横ばい／下落の確率、期待リターン、株価レンジ、モデルの検証成績を表示します。"),
    "valuation": ("理論株価", 5, "業種に合った評価方法による理論株価の範囲と、その前提・今の株価が前提としている成長率を表示します。"),
    "technicals": ("テクニカル分析", 4, "移動平均・モメンタム・ボラティリティ・出来高を表示します（株価データが必要）。"),
    "news": ("ニュース・開示", 4, "適時開示・8-Kなどの一次情報の件名と、原文へのリンクを表示します。"),
    "peers": ("競合比較", 4, "同じ業種の企業との指標の比較を表示します。"),
    "risk": ("リスク分析", 5, "最悪のシナリオ（下落率）、業種の過去の最大下落、注意すべきリスクを表示します。"),
    "commentary": ("AIによる総合コメント", 5, "保存済みの数値だけをもとに、AIが分析結果を分かりやすく説明します（新しい数値は作りません）。"),
}


@router.get("/{market}/{code}/sections", response_model=list[Section])
def sections(market: str, code: str, session: Session = Depends(get_session)) -> list[Section]:
    get_security(session, market, code)
    return [Section(name=n, data_status="not_implemented", available_in_phase=p, note=f"{d}現在は値を表示しません。")
            for n, p, d in SECTIONS.values()]
