from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import DataQualityCheck, MacroObservation, MacroSeries
from ..services.quality import staleness

router = APIRouter(prefix="/api/v1/macro", tags=["macro"])


def _value_at(obs: list[MacroObservation], d: date) -> float | None:
    """d 以前で最も新しい観測値。"""
    lo = [o for o in obs if o.obs_date <= d]
    return lo[-1].value if lo else None


@router.get("")
def summary(session: Session = Depends(get_session)) -> dict:
    out = []
    issues = {(q.subject, q.check_type) for q in session.scalars(select(DataQualityCheck).where(DataQualityCheck.dataset == "fred"))}
    for s in session.scalars(select(MacroSeries).order_by(MacroSeries.category, MacroSeries.key)):
        obs = list(session.scalars(select(MacroObservation).where(MacroObservation.series_key == s.key,
                                   MacroObservation.obs_date >= date.today() - timedelta(days=800)).order_by(MacroObservation.obs_date)))
        if not obs:
            out.append({"key": s.key, "name": s.name, "category": s.category, "data_status": "not_connected"})
            continue
        last = obs[-1]
        stale, age = staleness(last.obs_date, s.frequency)
        item = {"key": s.key, "name": s.name, "category": s.category, "unit": s.unit, "frequency": s.frequency,
                "source": f"{s.provider}（{s.source_id}）", "reliability": s.reliability, "note": s.note,
                "value": last.value, "obs_date": last.obs_date, "fetched_at": last.fetched_at, "age_days": age,
                "data_status": "stale" if stale else "ok",
                "prev_1m": _value_at(obs, last.obs_date - timedelta(days=30)),
                "prev_1y": _value_at(obs, last.obs_date - timedelta(days=365)),
                "outlier_flag": (s.key, "outlier") in issues}
        if s.key in ("us_cpi", "us_core_cpi") and item["prev_1y"]:
            item["yoy_pct"] = (last.value / item["prev_1y"] - 1) * 100  # 前年比（%）
        out.append(item)
    return {"series": out}


@router.get("/{key}/history")
def history(key: str, start: date | None = Query(None), session: Session = Depends(get_session)) -> dict:
    s = session.get(MacroSeries, key)
    if s is None:
        raise HTTPException(404, "系列が見つかりません")
    start = start or date.today() - timedelta(days=365 * 5)
    obs = session.scalars(select(MacroObservation).where(MacroObservation.series_key == key, MacroObservation.obs_date >= start)
                          .order_by(MacroObservation.obs_date))
    return {"key": key, "name": s.name, "unit": s.unit, "source": f"{s.provider}（{s.source_id}）",
            "points": [[o.obs_date.isoformat(), o.value] for o in obs]}
