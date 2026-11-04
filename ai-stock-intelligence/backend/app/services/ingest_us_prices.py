"""米国株の株価を、設定されたプロバイダー（tiingo / massive）から取得する（APIキー設定時のみ）。

障害時の切り替え（設計書 13章・14章）：
- 主の取得元（US_PRICE_PROVIDER）が使えないとき（認証・プランの拒否、または通信の障害で1件も取得できないとき）、
  代替の取得元（US_PRICE_FALLBACK、未指定ならキーのあるもう一方）に切り替えて取得する。
- 代替の取得元もない・使えないときは、取得を「失敗」として記録し、代わりの値は作らない。画面は保存済みの終値を
  基準日つきで表示し、取得元の障害であることを明示する（routers/securities.py の latest_quote）。
- 取得元が違っても日付が同じ終値は別の行として保存する（DailyPrice の主キーに provider を含む）。
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..providers.http import redact
from ..config import get_settings
from ..models import DailyPrice, DataQualityCheck, FinancialFact, IngestionRun, Security
from ..providers import us_prices
from .prices_store import store_prices

LABEL = {"tiingo": "Tiingo", "massive": "Massive（旧Polygon.io）"}


class ProviderDown(Exception):
    """取得元が使えない（認証の拒否、または1件も取得できない障害）。代替に切り替える理由。"""


def _tiingo(session: Session, st) -> tuple[int, list[str]]:
    client = us_prices.TiingoClient(st.tiingo_api_key, network_secret=st.via_network_secret("tiingo"))
    # 財務データを取得済みの米国株（SECの対象）から。無料プランの上限のため1回の実行は40銘柄まで
    us_ids = select(Security.id).where(Security.market == "US")
    ids = [i for (i,) in session.execute(select(FinancialFact.security_id).where(FinancialFact.security_id.in_(us_ids)).distinct())]
    secs = list(session.scalars(select(Security).where(Security.id.in_(ids)).order_by(Security.code)))
    last = dict(session.execute(select(DailyPrice.security_id, func.max(DailyPrice.trade_date))
                                .where(DailyPrice.provider == "tiingo").group_by(DailyPrice.security_id)).all())
    # 未取得の銘柄を優先し、その中では SEC_EXTRA_TICKERS（分析対象として指定した銘柄）を先に取る
    extra = [t.strip().upper() for t in st.sec_extra_tickers.split(",") if t.strip()]
    secs.sort(key=lambda s: (last.get(s.id) or date.min, extra.index(s.code) if s.code in extra else len(extra), s.code))
    total, errors, ok = 0, [], 0
    try:
        for s in secs[:40]:
            try:
                start = (last.get(s.id) or date.today() - timedelta(days=365 * 5)) + timedelta(days=1)
                total += store_prices(session, "US", client.daily(s.code, start), "tiingo", None)
                session.commit()
                ok += 1
            except PermissionError as e:
                raise ProviderDown(redact(str(e))) from None
            except Exception as e:
                session.rollback()
                errors.append(f"{s.code}: {redact(str(e))[:100]}")
    finally:
        client.close()
    if secs[:40] and ok == 0:  # 1銘柄も取得できなかった＝取得元の障害
        raise ProviderDown("; ".join(errors[:3]))
    return total, errors


def _massive(session: Session, st) -> tuple[int, list[str]]:
    client = us_prices.MassiveClient(st.massive_api_key, st.massive_base_url, network_secret=st.via_network_secret("massive"))
    have = {d for (d,) in session.execute(select(DailyPrice.trade_date).where(DailyPrice.provider == "massive").distinct())}
    d, fetched, ok, total, errors = date.today() - timedelta(days=1), 0, 0, 0, []
    try:
        while d >= date.today() - timedelta(days=730) and fetched < 60:
            if d.weekday() < 5 and d not in have:
                try:
                    total += store_prices(session, "US", client.grouped_daily(d), "massive", None)
                    session.commit()
                    ok += 1
                except PermissionError as e:
                    raise ProviderDown(redact(str(e))) from None
                except Exception as e:
                    session.rollback()
                    errors.append(f"{d}: {redact(str(e))[:100]}")
                fetched += 1
            d -= timedelta(days=1)
    finally:
        client.close()
    if fetched and ok == 0:
        raise ProviderDown("; ".join(errors[:3]))
    return total, errors


FETCH = {"tiingo": _tiingo, "massive": _massive}


def provider_chain(st) -> list[str]:
    """取得を試す順の取得元（キーのあるものだけ）。主 → 代替。"""
    norm = lambda p: "massive" if (p or "").strip().lower() == "polygon" else (p or "").strip().lower()
    primary, fallback = norm(st.us_price_provider), norm(st.us_price_fallback)
    if not fallback:  # 代替の指定がなければ、キーのあるもう一方
        fallback = {"tiingo": "massive", "massive": "tiingo"}.get(primary, "")
    chain = [p for p in (primary, fallback) if p in FETCH and st.has_credential(p)]
    return list(dict.fromkeys(chain))


def cross_check(session: Session) -> int:
    """同じ銘柄・同じ日の終値が取得元によって1%超違えば、品質の記録にする（どちらかの誤りの可能性）。"""
    a, b = DailyPrice.__table__.alias("a"), DailyPrice.__table__.alias("b")
    rows = session.execute(select(a.c.security_id, a.c.trade_date, a.c.close, b.c.close)
                           .where(a.c.security_id == b.c.security_id, a.c.trade_date == b.c.trade_date,
                                  a.c.provider == "tiingo", b.c.provider == "massive")).all()
    n = 0
    for sid, d, x, y in rows:
        if x and y and abs(x - y) / max(abs(x), abs(y)) > 0.01:
            session.add(DataQualityCheck(dataset="prices_us", subject=str(sid), check_type="inconsistent", severity="warn",
                                         detail={"date": str(d), "tiingo": x, "massive": y}))
            n += 1
    return n


def run(session: Session) -> IngestionRun:
    st = get_settings()
    chain = provider_chain(st)
    run = IngestionRun(dataset="prices_us", source=f"米国株の株価（{' → '.join(LABEL[p] for p in chain) or '未選択'}）",
                       status="running")
    session.add(run)
    session.commit()
    if not chain:
        run.status, run.finished_at = "skipped", datetime.now(timezone.utc)
        run.message = "米国株の株価APIが未設定（US_PRICE_PROVIDER と APIキー）"
        session.commit()
        return run
    total, errors, used, down = 0, [], None, []
    for prov in chain:
        try:
            total, errors = FETCH[prov](session, st)
            used = prov
            break
        except ProviderDown as e:
            session.rollback()
            down.append(f"{LABEL[prov]}：{str(e)[:150]}")
    run = session.get(IngestionRun, run.id)
    run.finished_at = datetime.now(timezone.utc)
    notes = []
    if used and down:
        notes.append(f"{'、'.join(d.split('：')[0] for d in down)} の障害のため {LABEL[used]} に切り替えて取得")
        run.source = f"米国株の株価（{LABEL[used]}・切り替え）"
    if used is None:
        run.status, run.rows = "failed", 0
        alt = "" if len(chain) > 1 else "（代替の取得元が未設定。US_PRICE_FALLBACK とそのAPIキーで設定できます）"
        notes.append(f"すべての取得元が使えないため取得しなかった{alt}。表示は保存済みの終値のまま")
    else:
        run.rows = total
        run.status = "success" if not errors else "partial"
        if cross_check(session):
            notes.append("取得元によって終値が1%超違う日があります（品質の記録を参照）")
    run.message = "; ".join(notes + down + errors[:5])[:1000] or None
    run.source_as_of = session.scalar(select(func.max(DailyPrice.trade_date)).where(DailyPrice.provider.in_(chain)))
    session.commit()
    return run
