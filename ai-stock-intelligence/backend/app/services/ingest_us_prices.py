"""米国株の株価を、設定されたプロバイダー（tiingo / massive）から取得する（APIキー設定時のみ）。"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import DailyPrice, FinancialFact, IngestionRun, Security
from ..providers import us_prices
from .prices_store import store_prices


def run(session: Session) -> IngestionRun:
    st = get_settings()
    prov = st.us_price_provider.strip().lower()
    prov = "massive" if prov == "polygon" else prov  # 旧名でも動くようにする
    run = IngestionRun(dataset="prices_us", source=f"米国株の株価（{prov or '未選択'}）", status="running")
    session.add(run)
    session.commit()
    key = {"tiingo": st.tiingo_api_key, "massive": st.massive_api_key, "polygon": st.massive_api_key}.get(prov, "")
    if not prov or not key:
        run.status, run.finished_at = "skipped", datetime.now(timezone.utc)
        run.message = "米国株の株価APIが未設定（US_PRICE_PROVIDER と APIキー）"
        session.commit()
        return run
    total, errors = 0, []
    if prov == "tiingo":
        client = us_prices.TiingoClient(key)
        # 財務データを取得済みの銘柄（SECの対象）から。無料プランの上限のため1回の実行は40銘柄まで
        ids = [i for (i,) in session.execute(select(FinancialFact.security_id).distinct())]
        secs = list(session.scalars(select(Security).where(Security.id.in_(ids)).order_by(Security.code)))
        last = dict(session.execute(select(DailyPrice.security_id, func.max(DailyPrice.trade_date))
                                    .where(DailyPrice.provider == "tiingo").group_by(DailyPrice.security_id)).all())
        secs.sort(key=lambda s: last.get(s.id) or date.min)
        try:
            for s in secs[:40]:
                try:
                    start = (last.get(s.id) or date.today() - timedelta(days=365 * 5)) + timedelta(days=1)
                    total += store_prices(session, "US", client.daily(s.code, start), "tiingo", None)
                    session.commit()
                except PermissionError as e:
                    errors.append(str(e))
                    break
                except Exception as e:
                    session.rollback()
                    errors.append(f"{s.code}: {str(e)[:100]}")
        finally:
            client.close()
    else:
        client = us_prices.MassiveClient(key, st.massive_base_url)
        have = {d for (d,) in session.execute(select(DailyPrice.trade_date).where(DailyPrice.provider == "massive").distinct())}
        d, fetched = date.today() - timedelta(days=1), 0
        try:
            while d >= date.today() - timedelta(days=730) and fetched < 60:
                if d.weekday() < 5 and d not in have:
                    try:
                        total += store_prices(session, "US", client.grouped_daily(d), "massive", None)
                        session.commit()
                    except PermissionError as e:
                        errors.append(str(e))
                        break
                    except Exception as e:
                        session.rollback()
                        errors.append(f"{d}: {str(e)[:100]}")
                    fetched += 1
                d -= timedelta(days=1)
        finally:
            client.close()
    run = session.get(IngestionRun, run.id)
    run.rows, run.finished_at = total, datetime.now(timezone.utc)
    run.status = "success" if not errors else ("partial" if total else "failed")
    run.message = "; ".join(errors[:5])[:1000] or None
    run.source_as_of = session.scalar(select(func.max(DailyPrice.trade_date)).where(DailyPrice.provider == prov))
    session.commit()
    return run
