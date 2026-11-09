"""米国株の株価を、設定されたプロバイダー（tiingo / massive）から取得する（APIキー設定時のみ）。

障害時の切り替え（設計書 13章・14章）：
- 主の取得元（US_PRICE_PROVIDER）が使えないとき（認証・プランの拒否、または通信の障害で1件も取得できないとき）、
  代替の取得元（US_PRICE_FALLBACK、未指定ならキーのあるもう一方）に切り替えて取得する。
- 代替の取得元もない・使えないときは、取得を「失敗」として記録し、代わりの値は作らない。画面は保存済みの終値を
  基準日つきで表示し、取得元の障害であることを明示する（routers/securities.py の latest_quote）。
- 取得元が違っても日付が同じ終値は別の行として保存する（DailyPrice の主キーに provider を含む）。

銘柄指定（codes。例 `python -m app.worker us_prices --codes AAPL`）：
- 指定した銘柄だけを Tiingo（銘柄ごとの日足）で取得し、指定した銘柄だけを保存する。取得期間・間隔は全銘柄の取得と同じ。
- Massive は使わない（grouped daily は市場全体を1回で取得するため、指定した銘柄だけの取得にならない）。
  Tiingo が使えなければ、Massive に切り替えずに失敗として止める。
- 銘柄一覧にない・形式が不正なコードは API に接続する前に止める。取得が0件なら成功にしない。既存の行は上書きしない。
no_fallback=True（--no-fallback）：全銘柄の取得でも、代替の取得元に切り替えない（主の取得元だけ）。
"""
import re
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


def _tiingo(session: Session, st, only_ids: list[int] | None = None) -> tuple[int, list[str]]:
    client = us_prices.TiingoClient(st.tiingo_api_key, network_secret=st.via_network_secret("tiingo"))
    if only_ids is None:
        # 財務データを取得済みの米国株（SECの対象）から。無料プランの上限のため1回の実行は40銘柄まで
        us_ids = select(Security.id).where(Security.market == "US")
        ids = [i for (i,) in session.execute(select(FinancialFact.security_id).where(FinancialFact.security_id.in_(us_ids)).distinct())]
    else:  # 銘柄指定：指定した銘柄だけ
        ids = list(only_ids)
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
                rows = client.daily(s.code, start)
                if only_ids is not None:
                    # 銘柄指定：指定した銘柄の行だけ。既存の行は上書きしない。0件は成功にしない
                    rows = [r for r in rows if r["code"] == s.code]
                    have = {d for (d,) in session.execute(select(DailyPrice.trade_date).where(
                        DailyPrice.security_id == s.id, DailyPrice.provider == "tiingo"))}
                    if not rows:
                        raise RuntimeError(f"指定した銘柄の株価が0件（開始日 {start}）")
                    rows = [r for r in rows if r["date"] not in have]
                total += store_prices(session, "US", rows, "tiingo", None)
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
        while d >= date.today() - timedelta(days=730) and fetched < max(1, st.massive_max_days):
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


def cross_check(session: Session, only_ids: list[int] | None = None) -> int:
    """同じ銘柄・同じ日の終値が取得元によって1%超違えば、品質の記録にする（どちらかの誤りの可能性）。
    only_ids を指定すると、その銘柄だけを照合する（銘柄指定の実行で、他の銘柄の記録を作らない）。"""
    a, b = DailyPrice.__table__.alias("a"), DailyPrice.__table__.alias("b")
    q = (select(a.c.security_id, a.c.trade_date, a.c.close, b.c.close)
         .where(a.c.security_id == b.c.security_id, a.c.trade_date == b.c.trade_date,
                a.c.provider == "tiingo", b.c.provider == "massive"))
    if only_ids is not None:
        q = q.where(a.c.security_id.in_(only_ids))
    rows = session.execute(q).all()
    n = 0
    for sid, d, x, y in rows:
        if x and y and abs(x - y) / max(abs(x), abs(y)) > 0.01:
            session.add(DataQualityCheck(dataset="prices_us", subject=str(sid), check_type="inconsistent", severity="warn",
                                         detail={"date": str(d), "tiingo": x, "massive": y}))
            n += 1
    return n


US_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def validate_codes(codes) -> list[str]:
    """米国株のティッカーの形式を確認する（大文字に）。不正・空は ValueError（API に接続する前に止める）。"""
    out = [str(c).strip().upper() for c in (codes or [])]
    if not out or any(not US_TICKER.match(c) for c in out):
        raise ValueError(f"米国株のティッカーが不正です：{', '.join(map(str, codes or [])) or '（空）'}")
    return list(dict.fromkeys(out))


def run(session: Session, codes: list[str] | None = None, no_fallback: bool = False) -> IngestionRun:
    st = get_settings()
    if codes is not None:
        codes = validate_codes(codes)  # 形式が不正なら、実行の記録も作らずに止める
    chain = provider_chain(st)
    if codes is not None:
        chain = [p for p in chain if p == "tiingo"]  # 銘柄指定は Tiingo だけ（Massive は市場全体を取得するため使わない）
    elif no_fallback:
        chain = chain[:1]  # 代替の取得元に切り替えない
    what = f"・銘柄指定 {','.join(codes)}" if codes else ("・代替への切り替えなし" if no_fallback else "")
    run = IngestionRun(dataset="prices_us", source=f"米国株の株価（{' → '.join(LABEL[p] for p in chain) or '未選択'}{what}）",
                       status="running")
    session.add(run)
    session.commit()
    only_ids = None
    if codes is not None:
        found = {c: i for c, i in session.execute(select(Security.code, Security.id).where(
            Security.market == "US", Security.code.in_(codes)))}
        missing = [c for c in codes if c not in found]
        why = (f"銘柄一覧にない米国株のティッカーのため取得しなかった：{', '.join(missing)}" if missing else
               "銘柄指定の取得は Tiingo だけで行う（Massive は市場全体を取得するため使わない）が、Tiingo の設定がない" if not chain else None)
        if why:  # API に接続せずに止める
            run.status, run.rows, run.message, run.finished_at = "failed", 0, why, datetime.now(timezone.utc)
            session.commit()
            return run
        only_ids = [found[c] for c in codes]
    if not chain:
        run.status, run.finished_at = "skipped", datetime.now(timezone.utc)
        run.message = "米国株の株価APIが未設定（US_PRICE_PROVIDER と APIキー）"
        session.commit()
        return run
    total, errors, used, down = 0, [], None, []
    for prov in chain:
        try:
            total, errors = _tiingo(session, st, only_ids) if only_ids is not None else FETCH[prov](session, st)
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
        alt = ("（銘柄指定・代替への切り替えなしの指定のため、代替の取得元は使わない）" if (only_ids is not None or no_fallback)
               else "" if len(chain) > 1 else "（代替の取得元が未設定。US_PRICE_FALLBACK とそのAPIキーで設定できます）")
        notes.append(f"すべての取得元が使えないため取得しなかった{alt}。表示は保存済みの終値のまま")
    else:
        run.rows = total
        # 全銘柄の取得は従来どおり（失敗があれば partial）。銘柄指定で1行も保存できなければ failed
        run.status = "success" if not errors else ("partial" if (total or only_ids is None) else "failed")
        if cross_check(session, only_ids):
            notes.append("取得元によって終値が1%超違う日があります（品質の記録を参照）")
    run.message = "; ".join(notes + down + errors[:5])[:1000] or None
    run.source_as_of = session.scalar(select(func.max(DailyPrice.trade_date)).where(DailyPrice.provider.in_(chain)))
    session.commit()
    return run
