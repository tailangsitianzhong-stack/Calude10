from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Security
from ..schemas import Quote, SearchResult, Section, SecurityDetail, SecurityOut
from ..services.text import normalize

router = APIRouter(prefix="/api/v1/securities", tags=["securities"])

CURRENCY = {"JP": "JPY", "US": "USD"}


def to_out(s: Security) -> SecurityOut:
    return SecurityOut(market=s.market, code=s.code, name=s.name, exchange=s.exchange, sector=s.sector,
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


@router.get("/{market}/{code}", response_model=SecurityDetail)
def detail(market: str, code: str, session: Session = Depends(get_session)) -> SecurityDetail:
    s = get_security(session, market, code)
    q = Quote(price=None, currency=CURRENCY.get(s.market, ""), as_of=None, provider=None, is_delayed=None,
              data_status="not_connected", note="株価データの取得はフェーズ3で実装予定")
    return SecurityDetail(security=to_out(s), quote=q)


# (表示名, 実装予定のフェーズ, 表示予定の内容)
SECTIONS = {
    "score": ("AI総合評価（100点満点）", 4, "9項目の採点内訳・項目ごとの信頼度・根拠・採点できなかった項目を表示します。"),
    "forecast": ("将来株価予測", 5, "1か月・3か月・6か月・1年の上昇／横ばい／下落の確率、期待リターン、株価レンジ、モデルの検証成績を表示します。"),
    "valuation": ("理論株価", 5, "業種に合った評価方法による理論株価の範囲と、その前提・今の株価が前提としている成長率を表示します。"),
    "financials": ("財務分析", 3, "売上・利益・キャッシュフロー・財務の安全性を、決算書の提出日つきで表示します。"),
    "technicals": ("テクニカル分析", 3, "移動平均・モメンタム・ボラティリティ・出来高を表示します。"),
    "news": ("ニュース・開示", 3, "適時開示・8-Kなどの一次情報の件名と、原文へのリンクを表示します。"),
    "peers": ("競合比較", 4, "同じ業種の企業との指標の比較を表示します。"),
    "risk": ("リスク分析", 5, "最悪のシナリオ（下落率）、業種の過去の最大下落、注意すべきリスクを表示します。"),
    "commentary": ("AIによる総合コメント", 5, "保存済みの数値だけをもとに、AIが分析結果を分かりやすく説明します（新しい数値は作りません）。"),
}


@router.get("/{market}/{code}/sections", response_model=list[Section])
def sections(market: str, code: str, session: Session = Depends(get_session)) -> list[Section]:
    get_security(session, market, code)
    return [Section(name=n, data_status="not_implemented", available_in_phase=p, note=f"{d}現在は値を表示しません。")
            for n, p, d in SECTIONS.values()]
