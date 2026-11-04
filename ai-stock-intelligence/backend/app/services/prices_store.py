"""株価・指数の保存（プロバイダー共通）。"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DailyPrice, IndexBar, Security


def store_prices(session: Session, market: str, rows: list[dict], provider: str, delay_minutes: int | None) -> int:
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    ids = {s.code: s.id for s in session.scalars(select(Security).where(Security.market == market))}
    dates = {r["date"] for r in rows}
    have = {(p.security_id, p.trade_date): p for p in session.scalars(select(DailyPrice).where(
        DailyPrice.provider == provider, DailyPrice.trade_date.in_(dates)))}
    n = 0
    for r in rows:
        sid = ids.get(r["code"])
        if sid is None:
            continue
        p = have.get((sid, r["date"]))
        if p is None:
            p = DailyPrice(security_id=sid, trade_date=r["date"], provider=provider)
            session.add(p)
        p.open, p.high, p.low, p.close = r.get("open"), r.get("high"), r.get("low"), r["close"]
        p.adj_close, p.volume, p.fetched_at, p.delay_minutes = r.get("adj_close"), r.get("volume"), now, delay_minutes
        n += 1
    return n


def store_index(session: Session, index_key: str, rows: list[dict], provider: str) -> int:
    now = datetime.now(timezone.utc)
    have = {b.trade_date: b for b in session.scalars(select(IndexBar).where(
        IndexBar.index_key == index_key, IndexBar.provider == provider))}
    for r in rows:
        b = have.get(r["date"])
        if b is None:
            b = IndexBar(index_key=index_key, trade_date=r["date"], provider=provider)
            session.add(b)
        b.open, b.high, b.low, b.close, b.fetched_at = r.get("open"), r.get("high"), r.get("low"), r["close"], now
    return len(rows)
