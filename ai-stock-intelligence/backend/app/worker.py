"""データ取得ワーカー（取得と分析を分離。設計書 2章）。

使い方:
    python -m app.worker fred            # FRED（金利・為替・経済指標・指数の終値）
    python -m app.worker sec [--limit N] # SECの財務データ
    python -m app.worker jquants         # J-Quants（APIキー設定時のみ）
    python -m app.worker jquants --codes 7203 --days 40   # 指定した銘柄だけ（TOPIX・他の銘柄は取得しない）
        # --days は「平日（月〜金）の数」。取引日の数ではない：祝日・休場日も数えるため、行数は指定より少なくなりうる
    python -m app.worker edinet --codes 7203,6861 --start 2025-10-01 --end 2026-06-30
    python -m app.worker edinet --all-listed --start 2026-06-01 --end 2026-06-15 --max-docs 200
                                         # 日本の普通株すべて。書類一覧を処理し終えた日は記録し、次回は取り直さない
                                         # EDINET の決算書類から負債の合計などを取得（EDINET_API_KEY 設定時のみ）
    python -m app.worker jquants_fins [--codes 7203,6758] [--dates 2026-08-07] [--max-dates N]
                                         # J-Quants の日本企業の財務情報（指定なしは未取得の開示日を N 日分）
    python -m app.worker us_prices       # 米国株の株価（APIキー設定時のみ）
    python -m app.worker us_prices --codes AAPL   # 指定した銘柄だけ（Tiingo のみ。Massive には切り替えない）
    python -m app.worker us_prices --no-fallback  # 全銘柄の取得で、代替の取得元に切り替えない
    python -m app.worker all             # 上記をすべて（失敗したものがあっても続行）
    python -m app.worker schedule        # 定期実行（Docker の worker サービスで使う）
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from .db import SessionLocal, init_db

log = logging.getLogger("worker")


def job(name: str, **kw) -> None:
    from .services import ingest_edinet, ingest_fred, ingest_jquants, ingest_jquants_fins, ingest_sec, ingest_us_prices
    fn = {"fred": ingest_fred.run, "sec": ingest_sec.run, "jquants": ingest_jquants.run, "us_prices": ingest_us_prices.run,
          "jquants_fins": ingest_jquants_fins.run, "edinet": ingest_edinet.run}[name]
    with SessionLocal() as s:
        try:
            r = fn(s, **kw)
            log.info("%s: status=%s rows=%s %s", name, r.status, r.rows, r.message or "")
        except Exception:
            log.exception("%s failed", name)


def schedule_intervals() -> dict[str, int]:
    """定期実行する処理と間隔（秒）。日本企業の財務情報は JQUANTS_FINS_SCHEDULE=true のときだけ（既定は無効）。"""
    from .config import get_settings
    intervals = {"fred": 6 * 3600, "sec": 24 * 3600, "jquants": 12 * 3600, "us_prices": 12 * 3600}
    if get_settings().jquants_fins_schedule:
        intervals["jquants_fins"] = 24 * 3600
    return intervals


def schedule() -> None:
    """簡易スケジューラ：取得元ごとの間隔で繰り返す（1つの失敗が他を止めない）。"""
    from .config import get_settings
    intervals = schedule_intervals()
    last: dict[str, float] = {}
    while True:
        now = time.time()
        for name, sec in intervals.items():
            if now - last.get(name, 0) >= sec:
                last[name] = now
                job(name, **({"max_dates": get_settings().jquants_fins_max_dates} if name == "jquants_fins" else {}))
        time.sleep(60)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    # httpx は要求URLをそのままログに出す（FRED の api_key などを含む）ため、警告以上だけにする
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["fred", "sec", "jquants", "us_prices", "jquants_fins", "edinet", "all", "schedule"])
    ap.add_argument("--start", default=None, help="edinet：提出日の開始 YYYY-MM-DD")
    ap.add_argument("--recheck", action="store_true", help="edinet：API に接続せず、保存済みの値から照合の品質の記録を作り直す")
    ap.add_argument("--all-listed", action="store_true",
                    help="edinet：日本の普通株すべて（上場中）を対象にする。--start・--end で期間を区切り、--max-docs で1回の上限を決める")
    ap.add_argument("--max-docs", type=int, default=None,
                    help="edinet：1回（1バッチ）あたりの書類の取得数の上限（--all-listed の既定は 200）")
    ap.add_argument("--end", default=None, help="edinet：提出日の終了 YYYY-MM-DD")
    ap.add_argument("--report", action="store_true",
                    help="edinet：API に接続せず、失敗の種類・負債なし・不一致率を集計して表示する（読み取りのみ）")
    ap.add_argument("--mismatch-stop-rate", type=float, default=None,
                    help="edinet：不一致率の停止基準（0 より大きく 1 以下。既定 0.10。照合できた書類が100件を超えてから判定）")
    ap.add_argument("--resume-after-mismatch-stop", action="store_true",
                    help="edinet：直前の実行が不一致率で停止したあと、原因を確認したうえで再開する（指定がなければ開始しない）")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--codes", default=None,
                    help="jquants_fins・jquants・us_prices：銘柄コード（カンマ区切り）。jquants・us_prices は指定した銘柄だけを取得・保存")
    ap.add_argument("--days", type=int, default=None,
                    help="jquants（--codes と一緒に）：取得する直近の平日（月〜金）の数。取引日の数ではない"
                         "（祝日・休場日も数えるため、株価の行数は指定より少なくなりうる）。範囲・上限は全銘柄の取得と同じ")
    ap.add_argument("--no-fallback", action="store_true",
                    help="us_prices：代替の取得元（Massive など）に切り替えない。--codes のときは常に Tiingo だけ")
    ap.add_argument("--dates", default=None, help="jquants_fins：開示日 YYYY-MM-DD（カンマ区切り）")
    ap.add_argument("--max-dates", type=int, default=None, help="jquants_fins：1回に取得する開示日の数")
    ap.add_argument("--dedupe-quality", action="store_true",
                    help="jquants_fins：API に接続せず、内容が同じ品質の記録の重複だけを削除する（財務の値には触れない）")
    ap.add_argument("--quality-only", action="store_true", help="jquants_fins：API に接続せず、保存済みの開示から品質の記録だけを作る")
    a = ap.parse_args()
    # 銘柄指定の確認（API・DB に触れる前に止める）
    codes = [c.strip() for c in a.codes.split(",") if c.strip()] if a.codes is not None else None
    if a.task in ("jquants", "us_prices") and codes is not None:
        from .services import ingest_jquants, ingest_us_prices
        try:
            codes = (ingest_jquants if a.task == "jquants" else ingest_us_prices).validate_codes(codes)
        except ValueError as e:
            ap.error(str(e))
    if a.days is not None and (a.task != "jquants" or codes is None or a.days < 1):
        ap.error("--days は jquants で --codes と一緒に、1以上で指定してください")
    if a.no_fallback and a.task != "us_prices":
        ap.error("--no-fallback は us_prices で指定してください")
    if a.mismatch_stop_rate is not None:  # 不正な値は、DB の準備や API への接続の前に拒否する
        from .services.ingest_edinet import validate_stop_rate
        try:
            validate_stop_rate(a.mismatch_stop_rate)
        except ValueError as e:
            ap.error(str(e))
    if not (a.task == "edinet" and a.report):  # 集計は読み取りのみ（テーブルの作成・列の追加もしない）
        init_db()
    log.info("start %s at %s", a.task, datetime.now(timezone.utc).isoformat())
    if a.task == "schedule":
        schedule()
    elif a.task == "all":
        for n in ("fred", "sec", "jquants", "us_prices"):
            job(n)
    elif a.task == "sec":
        job("sec", limit=a.limit)
    elif a.task == "edinet":
        from datetime import date as _d
        if a.report:
            import json
            from .services.ingest_edinet import report
            with SessionLocal() as s:
                print(json.dumps(report(s), ensure_ascii=False, indent=2))
                s.rollback()
        elif a.recheck:
            job("edinet", recheck_only=True)
        else:
            if not (a.start and a.end) or not (a.codes or a.all_listed) or (a.codes and a.all_listed):
                ap.error("edinet には --start・--end と、--codes か --all-listed のどちらか一方が必要です")
            if a.max_docs is not None and a.max_docs < 1:
                ap.error("--max-docs は1以上を指定してください")
            job("edinet", codes=[c.strip() for c in a.codes.split(",") if c.strip()] if a.codes else None,
                start=_d.fromisoformat(a.start), end=_d.fromisoformat(a.end), all_listed=a.all_listed, max_docs=a.max_docs,
                resume_after_mismatch_stop=a.resume_after_mismatch_stop,
                **({"mismatch_stop_rate": a.mismatch_stop_rate} if a.mismatch_stop_rate is not None else {}))
    elif a.task == "jquants_fins" and a.dedupe_quality:
        from .services.quality import remove_duplicates
        with SessionLocal() as s:
            log.info("quality dedupe: removed %s duplicate rows", remove_duplicates(s))
    elif a.task == "jquants_fins":
        from datetime import date as _d
        job("jquants_fins", codes=[c.strip() for c in a.codes.split(",") if c.strip()] if a.codes else None,
            dates=[_d.fromisoformat(x.strip()) for x in a.dates.split(",") if x.strip()] if a.dates else None,
            max_dates=a.max_dates, quality_only=a.quality_only)
    elif a.task == "jquants" and codes is not None:
        job("jquants", codes=codes, days=a.days)
    elif a.task == "us_prices" and (codes is not None or a.no_fallback):
        job("us_prices", codes=codes, no_fallback=a.no_fallback)
    else:
        job(a.task)


if __name__ == "__main__":
    main()
