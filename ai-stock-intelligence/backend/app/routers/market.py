from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import IndexBar
from ..providers import jquants
from ..schemas import IndexQuote
from ..services.quality import staleness

router = APIRouter(prefix="/api/v1/market", tags=["market"])

# (キー, 名前, 市場, プロバイダーの優先順)
INDICES = (("nikkei225", "日経平均株価", "JP", ("FRED",)), ("topix", "TOPIX", "JP", ("jquants",)),
           ("sp500", "S&P 500", "US", ("FRED",)), ("nasdaq", "NASDAQ総合", "US", ("FRED",)))
PROVIDER_LABEL = {"FRED": "FRED（各指数の提供元のデータ）", "jquants": "J-Quants API V2"}


def _delay_note(provider: str) -> str:
    if provider == "FRED":
        return "終値（FREDは翌営業日に更新）。リアルタイムではありません"
    plan = get_settings().jquants_plan
    return jquants.DELAY_NOTE.get(plan, "")


@router.get("/overview", response_model=list[IndexQuote])
def overview(session: Session = Depends(get_session)) -> list[IndexQuote]:
    out = []
    for key, name, mkt, provs in INDICES:
        bars = list(session.scalars(select(IndexBar).where(IndexBar.index_key == key, IndexBar.provider.in_(provs))
                                    .order_by(IndexBar.trade_date.desc()).limit(2)))
        if not bars:
            st = get_settings()
            if key == "topix" and st.has_credential("jquants") and st.jquants_plan == "free":
                note = "J-Quants の Free プランでは TOPIX は提供されていません（Light 以上で取得可能）"
            elif key == "topix":
                note = "J-Quants API キー未設定のため未接続"
            else:
                note = "未取得"
            out.append(IndexQuote(key=key, name=name, market=mkt, value=None, change_pct=None, as_of=None, provider=None,
                                  is_delayed=None, data_status="not_connected", note=note))
            continue
        last = bars[0]
        chg = (last.close / bars[1].close - 1) * 100 if len(bars) > 1 and bars[1].close else None
        stale, _ = staleness(last.trade_date, "daily")
        out.append(IndexQuote(key=key, name=name, market=mkt, value=last.close, change_pct=chg, as_of=last.trade_date,
                              provider=PROVIDER_LABEL.get(last.provider, last.provider), is_delayed=True,
                              data_status="stale" if stale else "ok", note=_delay_note(last.provider)))
    return out


@router.get("/indices/{key}/history")
def index_history(key: str, start: date | None = Query(None), session: Session = Depends(get_session)) -> dict:
    meta = next((i for i in INDICES if i[0] == key), None)
    if meta is None:
        raise HTTPException(404, "指数が見つかりません")
    start = start or date.today() - timedelta(days=365)
    bars = session.scalars(select(IndexBar).where(IndexBar.index_key == key, IndexBar.provider.in_(meta[3]),
                                                  IndexBar.trade_date >= start).order_by(IndexBar.trade_date))
    return {"key": key, "name": meta[1], "points": [[b.trade_date.isoformat(), b.close] for b in bars]}
