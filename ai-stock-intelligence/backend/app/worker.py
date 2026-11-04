"""データ取得ワーカー（取得と分析を分離。設計書 2章）。

使い方:
    python -m app.worker fred            # FRED（金利・為替・経済指標・指数の終値）
    python -m app.worker sec [--limit N] # SECの財務データ
    python -m app.worker jquants         # J-Quants（APIキー設定時のみ）
    python -m app.worker us_prices       # 米国株の株価（APIキー設定時のみ）
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
    from .services import ingest_fred, ingest_jquants, ingest_sec, ingest_us_prices
    fn = {"fred": ingest_fred.run, "sec": ingest_sec.run, "jquants": ingest_jquants.run, "us_prices": ingest_us_prices.run}[name]
    with SessionLocal() as s:
        try:
            r = fn(s, **kw)
            log.info("%s: status=%s rows=%s %s", name, r.status, r.rows, r.message or "")
        except Exception:
            log.exception("%s failed", name)


def schedule() -> None:
    """簡易スケジューラ：取得元ごとの間隔で繰り返す（1つの失敗が他を止めない）。"""
    intervals = {"fred": 6 * 3600, "sec": 24 * 3600, "jquants": 12 * 3600, "us_prices": 12 * 3600}
    last: dict[str, float] = {}
    while True:
        now = time.time()
        for name, sec in intervals.items():
            if now - last.get(name, 0) >= sec:
                last[name] = now
                job(name)
        time.sleep(60)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=["fred", "sec", "jquants", "us_prices", "all", "schedule"])
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    init_db()
    log.info("start %s at %s", a.task, datetime.now(timezone.utc).isoformat())
    if a.task == "schedule":
        schedule()
    elif a.task == "all":
        for n in ("fred", "sec", "jquants", "us_prices"):
            job(n)
    elif a.task == "sec":
        job("sec", limit=a.limit)
    else:
        job(a.task)


if __name__ == "__main__":
    main()
