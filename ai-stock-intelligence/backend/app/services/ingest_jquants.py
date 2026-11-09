"""J-Quants から日本株の株価と TOPIX を取得する（APIキー設定時のみ）。

Freeプランは5リクエスト/分のため、日付ごとの一括取得（1日1リクエスト＋ページ送り）で、直近から遡って取得する。
プランは Settings.jquants_fetch_plan：確認済みならそのプラン、JQUANTS_PLAN 未設定なら Free 相当の安全側の条件
（従来どおり）。設定値が不正なら取得しない（設定不備。ほかのデータ連携には影響しない）。
TOPIX の取得結果（成功／API がプランの範囲外と応答／そのほかの失敗）は構造化して残す（services/jquants_plan.py）。

銘柄指定（codes。例 `python -m app.worker jquants --codes 7203 --days 40`）：
- 指定した銘柄だけを、銘柄ごとの日足（/v2/equities/bars/daily?code=…）で取得し、指定した銘柄だけを保存する。
  TOPIX や他の銘柄は取得・保存しない。
- 取得期間・遅延・アクセス間隔は全銘柄の取得と同じ（最新は今日−プランの遅延、最古は730日前、
  1回の日数の上限は MAX_DAYS_PER_RUN）。days はその範囲の直近の「平日（月〜金）」の数で、取引日の数ではない
  （祝日・市場の休場日も数える。API はその日の行を返さないので、株価の行数は days より少なくなりうる）。
- 銘柄一覧にない・形式が不正なコードは、API に接続する前に止める。取得が0件なら成功にしない。
- 既存の株価の行は上書き・削除しない（同じ銘柄・日付・取得元の行がすでにあれば保存しない）。
"""
import re
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..providers.http import redact
from ..config import get_settings
from ..models import DailyPrice, IngestionRun
from ..providers import jquants
from . import jquants_plan
from .prices_store import store_index, store_prices

log = logging.getLogger(__name__)

MAX_DAYS_PER_RUN = {"free": 40, "light": 400, "standard": 600, "premium": 1000}
# 4桁（英字を含む場合あり。例 7203、167A）。数字と紛らわしい B・E・I・O・Q・V・Z は証券コードに使われない
JP_CODE = re.compile(r"^[0-9][0-9ACDFGHJKLMNPRSTUWXY]{3}$")


def validate_codes(codes) -> list[str]:
    """銘柄コードの形式を確認する（4桁。英字は大文字に）。不正・空・重複は ValueError（API に接続する前に止める）。"""
    out = [str(c).strip().upper() for c in (codes or [])]
    if not out or any(not JP_CODE.match(c) for c in out):
        raise ValueError(f"日本株の銘柄コードが不正です（4桁で指定してください）：{', '.join(map(str, codes or [])) or '（空）'}")
    return list(dict.fromkeys(out))


def run(session: Session, codes: list[str] | None = None, days: int | None = None) -> IngestionRun:
    st = get_settings()
    if codes is not None:
        codes = validate_codes(codes)  # 形式が不正なら、実行の記録も作らずに止める
        if days is not None and days < 1:
            raise ValueError("--days は1以上を指定してください")
    elif days is not None:
        raise ValueError("--days は --codes と一緒に指定してください")
    label = jquants_plan.run_source_label(st) + (f"・銘柄指定 {','.join(codes)}" if codes else "")
    run = IngestionRun(dataset="prices_jp", source=f"J-Quants API V2（{label}）", status="running")
    session.add(run)
    session.commit()
    if not st.has_credential("jquants"):
        run.status, run.message, run.finished_at = "skipped", "APIキー未設定（JQUANTS_API_KEY またはネットワークシークレット）", datetime.now(timezone.utc)
        session.commit()
        return run
    if st.jquants_fetch_plan is None:  # 設定不備：Free に置き換えずに取得を止める（API に接続しない）
        log.error("J-Quants の取得を停止：%s", jquants_plan.invalid_message(st))
        run.status, run.message, run.finished_at = "skipped", jquants_plan.invalid_message(st)[:1000], datetime.now(timezone.utc)
        session.commit()
        return run
    plan = st.jquants_fetch_plan
    if codes is not None:
        return _run_codes(session, run, st, plan, codes, days)
    client = jquants.JQuantsClient(st.jquants_api_key, plan, network_secret=st.via_network_secret("jquants"))
    total, errors, notes = 0, [], []
    try:
        try:
            n = store_index(session, "topix", jquants.parse_index_bars(client.topix(date.today() - timedelta(days=800))), "jquants")
            total += n
            session.commit()
            jquants_plan.save_topix_result(session, run.id, "ok", rows=n, plan_status=st.jquants_plan_status)
        except jquants.PlanNotAvailable:
            session.rollback()
            notes.append("TOPIX はご契約のプランでは提供されていません（API の応答）")
            jquants_plan.save_topix_result(session, run.id, "plan_not_available", plan_status=st.jquants_plan_status)
        except Exception as e:
            session.rollback()
            errors.append(f"TOPIX: {redact(str(e))[:150]}")
            jquants_plan.save_topix_result(session, run.id, "error", error=redact(str(e)), plan_status=st.jquants_plan_status)
        session.commit()
        delay = jquants.DELAY_DAYS.get(plan, 0)
        newest = date.today() - timedelta(days=delay)
        have = {d for (d,) in session.execute(select(DailyPrice.trade_date).where(DailyPrice.provider == "jquants").distinct())}
        oldest = date.today() - timedelta(days=730)
        d, fetched = newest, 0
        while d >= oldest and fetched < MAX_DAYS_PER_RUN.get(plan, 40):
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


def _window(plan: str, days: int | None, today: date | None = None) -> tuple[date, date]:
    """銘柄指定の取得期間：全銘柄の取得と同じ範囲（最新＝今日−プランの遅延、最古＝730日前、日数の上限＝MAX_DAYS_PER_RUN）
    のうち、直近の平日 days 日（未指定なら上限の日数）。（開始日, 終了日）を返す。"""
    today = today or date.today()
    newest = today - timedelta(days=jquants.DELAY_DAYS.get(plan, 0))
    oldest = today - timedelta(days=730)
    n = min(days or MAX_DAYS_PER_RUN.get(plan, 40), MAX_DAYS_PER_RUN.get(plan, 40))
    d, start, count = newest, newest, 0
    while d >= oldest and count < n:
        if d.weekday() < 5:
            start, count = d, count + 1
        d -= timedelta(days=1)
    return start, newest


def _run_codes(session: Session, run: IngestionRun, st, plan: str, codes: list[str], days: int | None) -> IngestionRun:
    """指定した銘柄だけを取得・保存する（TOPIX・他の銘柄は取得しない。既存の行は上書きしない）。"""
    from ..models import Security

    def finish(status: str, rows: int, message: str) -> IngestionRun:
        r = session.get(IngestionRun, run.id)
        r.status, r.rows, r.message, r.finished_at = status, rows, message[:1000], datetime.now(timezone.utc)
        r.source_as_of = session.scalar(select(func.max(DailyPrice.trade_date)).where(
            DailyPrice.provider == "jquants", DailyPrice.security_id.in_(list(secs.values()) or [-1])))
        session.commit()
        return r

    secs = {c: i for c, i in session.execute(select(Security.code, Security.id).where(
        Security.market == "JP", Security.code.in_(codes)))}
    missing = [c for c in codes if c not in secs]
    if missing:  # 銘柄一覧にないコード：API に接続せずに止める
        return finish("failed", 0, f"銘柄一覧にない日本株のコードのため取得しなかった：{', '.join(missing)}")
    start, end = _window(plan, days)
    delay = jquants.DELAY_DAYS.get(plan, 0)
    client = jquants.JQuantsClient(st.jquants_api_key, plan, network_secret=st.via_network_secret("jquants"))
    total, errors, empty = 0, [], []
    try:
        for code in codes:
            try:
                rows = jquants.parse_daily_bars(client.daily_bars_by_code(code, start, end))
            except (PermissionError, jquants.PlanNotAvailable) as e:
                errors.append(f"{code}: {redact(str(e))[:150]}")
                break
            except Exception as e:
                errors.append(f"{code}: {redact(str(e))[:150]}")
                continue
            # 念のため、指定した銘柄・期間の行だけに絞る（他の銘柄の行は保存しない）
            rows = [r for r in rows if r["code"] == code and start <= r["date"] <= end]
            have = {d for (d,) in session.execute(select(DailyPrice.trade_date).where(
                DailyPrice.security_id == secs[code], DailyPrice.provider == "jquants"))}
            new = [r for r in rows if r["date"] not in have]  # 既存の行は上書きしない
            if not rows:
                empty.append(code)
            if new:
                total += store_prices(session, "JP", new, "jquants", delay * 24 * 60 if delay else None)
                session.commit()
    finally:
        client.close()
    period = f"期間 {start}〜{end}"
    if errors:
        return finish("partial" if total else "failed", total, f"{period}; " + "; ".join(errors[:5]))
    if empty:  # 指定した銘柄の株価が0件：成功にしない
        return finish("failed", total, f"{period}; 指定した銘柄の株価が0件：{', '.join(empty)}")
    return finish("success", total, f"{period}; 銘柄指定 {', '.join(codes)}（既存の行は保存しない）")
