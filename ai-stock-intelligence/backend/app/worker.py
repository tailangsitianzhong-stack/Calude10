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
    python -m app.worker check-db        # 接続先（DATABASE_URL）の検査と識別情報の表示（DB には書き込まない）
    python -m app.worker check-db --api-url http://127.0.0.1:8000   # API と同じ DB を使っているかを比べる
    python -m app.worker init-db --create-new-sqlite   # 初回セットアップ：まだない SQLite のDBを新しく作る

接続先は DATABASE_URL（SQLite は絶対パス）。未設定・相対パス・存在しないDBなら、DB に触れる前に止まる。
環境変数 DB_CHECK_API_URL を設定すると、起動時に API と同じ DB を使っているかを確かめ、違えば止まる。
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from .db import DatabaseConfigError, SessionLocal, db_identity, get_engine, init_db

log = logging.getLogger("worker")


def job(name: str, **kw) -> None:
    from .services import ingest_edinet, ingest_fred, ingest_jquants, ingest_jquants_fins, ingest_sec, ingest_us_prices
    fn = {"fred": ingest_fred.run, "sec": ingest_sec.run, "jquants": ingest_jquants.run, "us_prices": ingest_us_prices.run,
          "jquants_fins": ingest_jquants_fins.run, "edinet": ingest_edinet.run}[name]
    get_engine()
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


def api_db_fingerprint(api_url: str, attempts: int = 3) -> str | None:
    """API の /healthz から、API が使っている DB の fingerprint を読む（読めなければ None）。"""
    import httpx
    for i in range(attempts):
        try:
            r = httpx.get(api_url.rstrip("/") + "/healthz", timeout=10, trust_env=False)
            return (r.json().get("db") or {}).get("fingerprint")
        except (httpx.HTTPError, ValueError, AttributeError):
            if i + 1 < attempts:
                time.sleep(2)
    return None


def check_db(api_url: str | None, quiet: bool = False) -> int:
    """接続先を検査して識別情報を表示する。api_url があれば API の DB と比べる。0＝問題なし、2＝誤り・不一致。"""
    try:
        get_engine()
    except DatabaseConfigError as e:
        log.error("データベースの設定の誤り：%s", e)
        return 2
    ident = db_identity()
    if not quiet:
        print(json.dumps(ident, ensure_ascii=False, indent=2))
    if api_url:
        theirs = api_db_fingerprint(api_url)
        if theirs is None:
            log.error("API（%s）の DB を確認できない。同じ DB を使っているか判定できないため止める", api_url)
            return 2
        if theirs != ident["fingerprint"]:
            log.error("API とワーカーの DB が異なる（API %s／ワーカー %s）。DATABASE_URL を同じ値にする", theirs, ident["fingerprint"])
            return 2
        log.info("API とワーカーは同じ DB を使っている（%s）", theirs)
    return 0


def init_database(create_new_sqlite: bool) -> int:
    """初回セットアップ。SQLite のDBがまだないときに、--create-new-sqlite の指定があれば新しく作ってテーブルを用意する。
    すでに存在するDBには何もしない（上書き・初期化しない）。フォルダは作らない。"""
    from . import db as dbmod
    from .config import get_settings
    url = get_settings().database_url
    try:
        p = dbmod.sqlite_path(dbmod.validate_url(url, create=True))
    except DatabaseConfigError as e:
        log.error("データベースの設定の誤り：%s", e)
        return 2
    if p is None:
        log.error("init-db は SQLite のファイルを新しく作るときだけ使う（PostgreSQL は docker compose が用意する）")
        return 2
    if p.exists():
        log.error("DB はすでに存在する（%s）。上書き・初期化はしない", p)
        return 2
    if not create_new_sqlite:
        log.error("新しいDBを作るには --create-new-sqlite を指定する（保存先 %s）", p)
        return 2
    dbmod.engine = dbmod.make_engine(url, create=True)
    init_db()
    log.info("新しいDBを作成した（%s）", p)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    # httpx は要求URLをそのままログに出す（FRED の api_key などを含む）ため、警告以上だけにする
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["fred", "sec", "jquants", "us_prices", "jquants_fins", "edinet", "all", "schedule",
                                     "check-db", "init-db"])
    ap.add_argument("--api-url", default=None, help="check-db：比べる API の URL（例 http://127.0.0.1:8000）")
    ap.add_argument("--create-new-sqlite", action="store_true",
                    help="init-db：まだない SQLite のDBを新しく作る（初回セットアップだけ。既存のDBには何もしない）")
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
    if a.task == "init-db":
        sys.exit(init_database(a.create_new_sqlite))
    if a.task == "check-db":
        sys.exit(check_db(a.api_url))
    # DB の接続先の検査（API・DB に触れる前に止める。既定の DB に黙って切り替えない）
    try:
        get_engine()
    except DatabaseConfigError as e:
        log.error("データベースの設定の誤り：%s", e)
        sys.exit(2)
    api_url = os.environ.get("DB_CHECK_API_URL", "").strip()
    if api_url and check_db(api_url, quiet=True) != 0:
        sys.exit(2)
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
