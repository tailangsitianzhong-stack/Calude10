"""J-Quants から日本株の株価と TOPIX を取得する（APIキー設定時のみ）。

Freeプランは5リクエスト/分のため、日付ごとの一括取得（1日1リクエスト＋ページ送り）で、直近から遡って取得する。
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..providers.http import redact
from ..config import get_settings
from ..models import DailyPrice, IngestionRun
from ..providers import jquants
from .prices_store import store_index, store_prices

MAX_DAYS_PER_RUN = {"free": 40, "light": 400, "standard": 600, "premium": 1000}


def run(session: Session) -> IngestionRun:
    st = get_settings()
    run = IngestionRun(dataset="prices_jp", source=f"J-Quants API V2（{st.jquants_plan}）", status="running")
    session.add(run)
    session.commit()
    if not st.has_credential("jquants"):
        run.status, run.message, run.finished_at = "skipped", "APIキー未設定（JQUANTS_API_KEY またはネットワークシークレット）", datetime.now(timezone.utc)
        session.commit()
        return run
    client = jquants.JQuantsClient(st.jquants_api_key, st.jquants_plan, network_secret=st.via_network_secret("jquants"))
    total, errors, notes = 0, [], []
    try:
        try:
            total += store_index(session, "topix", jquants.parse_index_bars(client.topix(date.today() - timedelta(days=800))), "jquants")
            session.commit()
        except jquants.PlanNotAvailable:
            session.rollback()
            notes.append("TOPIX はご契約のプランでは提供されていません（Light以上で取得可能）")
        except Exception as e:
            session.rollback()
            errors.append(f"TOPIX: {redact(str(e))[:150]}")
        delay = jquants.DELAY_DAYS.get(st.jquants_plan, 0)
        newest = date.today() - timedelta(days=delay)
        have = {d for (d,) in session.execute(select(DailyPrice.trade_date).where(DailyPrice.provider == "jquants").distinct())}
        oldest = date.today() - timedelta(days=730)
        d, fetched = newest, 0
        while d >= oldest and fetched < MAX_DAYS_PER_RUN.get(st.jquants_plan, 40):
            if d.weekday() < 5 and d not in have:
                try:
                    rows = jquants.parse_daily_bars(client.daily_bars_by_date(d))
                    total += store_prices(session, "JP", rows, "jquants", delay * 24 * 60 if delay else None)
                    session.commit()
                except (PermissionError, jquants.PlanNotAvailable) as e:
                    errors.append(redact(str(e)))
                    break
                except Exception as e:
                    session.rollback()
                    errors.append(f"{d}: {redact(str(e))[:100]}")
                fetched += 1
            d -= timedelta(days=1)
    finally:
        client.close()
    run = session.get(IngestionRun, run.id)
    run.rows, run.finished_at = total, datetime.now(timezone.utc)
    run.status = "success" if not errors else ("partial" if total else "failed")
    if run.status == "failed":  # 日本株の株価は J-Quants だけ。代わりの値は作らず、障害として記録する
        notes.append("日本株の株価に代わりの取得元はないため、取得しなかった（表示は保存済みの終値のまま）")
    run.message = "; ".join(notes + errors[:5])[:1000] or None
    run.source_as_of = session.scalar(select(func.max(DailyPrice.trade_date)).where(DailyPrice.provider == "jquants"))
    session.commit()
    return run
