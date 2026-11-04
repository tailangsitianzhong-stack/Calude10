"""FREDの系列を取得してデータベースに保存する。"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import DataQualityCheck, IndexBar, IngestionRun, MacroObservation, MacroSeries
from ..providers import fred
from .quality import outliers, staleness

RELIABILITY = "公的統計・公式データ（FRED経由）"
REVISION_WINDOW_DAYS = 120  # 改定に備え、最新の観測日から遡って再取得する日数


def ensure_series(session: Session) -> None:
    existing = {k for (k,) in session.execute(select(MacroSeries.key))}
    for s in fred.SERIES:
        if s.key not in existing:
            session.add(MacroSeries(key=s.key, source_id=s.fred_id, name=s.name, category=s.category, unit=s.unit,
                                    frequency=s.frequency, provider="FRED", reliability=RELIABILITY, note=s.note or None))
    session.commit()


def ingest_series(session: Session, client: fred.FredClient, sd: fred.SeriesDef) -> int:
    last = session.scalar(select(func.max(MacroObservation.obs_date)).where(MacroObservation.series_key == sd.key))
    start = last - timedelta(days=REVISION_WINDOW_DAYS) if last else None
    rows, missing = client.fetch(sd.fred_id, start)
    now = datetime.now(timezone.utc)
    if not rows:
        return 0
    # 注意：月次・四半期の系列では、FREDは cosd を含む期間の初日（例：月初）から返すため、cosd より前の日付が含まれる
    first = min(d for d, _ in rows)
    have = {o.obs_date: o for o in session.scalars(select(MacroObservation).where(
        MacroObservation.series_key == sd.key, MacroObservation.obs_date >= first))}
    changed = 0
    for d, v in rows:
        o = have.get(d)
        if o is None:
            session.add(MacroObservation(series_key=sd.key, obs_date=d, value=v, fetched_at=now))
            changed += 1
        elif o.value != v:  # 改定された値は上書きし、取得日時を更新する
            o.value, o.fetched_at = v, now
            changed += 1
    if sd.key in fred.INDEX_KEYS:
        ik = fred.INDEX_KEYS[sd.key]
        bars = {b.trade_date: b for b in session.scalars(select(IndexBar).where(
            IndexBar.index_key == ik, IndexBar.provider == "FRED", IndexBar.trade_date >= first))}
        for d, v in rows:
            b = bars.get(d)
            if b is None:
                session.add(IndexBar(index_key=ik, trade_date=d, close=v, provider="FRED", fetched_at=now))
            elif b.close != v:
                b.close, b.fetched_at = v, now
    session.flush()
    # 品質の検証
    series = [(o.obs_date, o.value) for o in session.scalars(
        select(MacroObservation).where(MacroObservation.series_key == sd.key).order_by(MacroObservation.obs_date))]
    last_obs = series[-1][0] if series else None
    stale, age = staleness(last_obs, sd.frequency)
    if stale:
        session.add(DataQualityCheck(dataset="fred", subject=sd.key, check_type="stale", severity="warn",
                                     detail={"last_obs": str(last_obs), "age_days": age}))
    for o in outliers(series):
        session.add(DataQualityCheck(dataset="fred", subject=sd.key, check_type="outlier", severity="info", detail=o))
    if missing and not start:  # 初回の全期間取得時だけ記録（休日の欠損は正常）
        session.add(DataQualityCheck(dataset="fred", subject=sd.key, check_type="missing", severity="info",
                                     detail={"missing_values": missing, "note": "休日等の欠損（'.'）を除外"}))
    return changed


def run(session: Session, keys: list[str] | None = None) -> IngestionRun:
    ensure_series(session)
    run = IngestionRun(dataset="fred", source="FRED fredgraph.csv", status="running")
    session.add(run)
    session.commit()
    if not keys:  # 全系列を取り直すときは、前回の検証結果を置き換える
        session.execute(delete(DataQualityCheck).where(DataQualityCheck.dataset == "fred"))
        session.commit()
    client = fred.FredClient()
    total, errors = 0, []
    try:
        for sd in fred.SERIES:
            if keys and sd.key not in keys:
                continue
            try:
                total += ingest_series(session, client, sd)
                session.commit()
            except Exception as e:  # 1系列の失敗で全体を止めない
                session.rollback()
                errors.append(f"{sd.key}: {str(e)[:120]}")
    finally:
        client.close()
    run = session.get(IngestionRun, run.id)
    run.rows, run.finished_at = total, datetime.now(timezone.utc)
    run.status = "success" if not errors else ("partial" if total or len(errors) < len(fred.SERIES) else "failed")
    run.message = "; ".join(errors)[:1000] or None
    run.source_as_of = session.scalar(select(func.max(MacroObservation.obs_date)))
    session.commit()
    return run
