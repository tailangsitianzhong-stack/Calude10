"""銘柄マスタを公式の一覧から取り込む。

使い方（backend ディレクトリで）:
    python -m scripts.load_securities            # インターネットから取得
    python -m scripts.load_securities --dir PATH # 手元のファイル（data_j.xlsx, nasdaqlisted.txt, otherlisted.txt）
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import IngestionRun  # noqa: E402
from app.services import security_master as sm  # noqa: E402

URLS = {
    "data_j.xlsx": "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx",
    "nasdaqlisted.txt": "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "otherlisted.txt": "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
}


def fetch(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60, headers={"User-Agent": "AI-STOCK-INTELLIGENCE/0.2"}) as c:
        for name, url in URLS.items():
            r = c.get(url)
            r.raise_for_status()
            (dest / name).write_bytes(r.content)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=None)
    args = ap.parse_args()
    src = args.dir or Path("data/raw")
    init_db()
    with SessionLocal() as s:
        try:
            if args.dir is None:
                fetch(src)
            rows, as_of = sm.parse_jpx(src / "data_j.xlsx")
            run = sm.upsert(s, rows, sm.JPX_SOURCE, as_of, "securities_jp")
            print(f"日本株：{run.rows}件（基準日 {as_of}）")
            rows, as_of = sm.parse_nasdaq(src / "nasdaqlisted.txt", src / "otherlisted.txt")
            run = sm.upsert(s, rows, sm.NASDAQ_SOURCE, as_of, "securities_us")
            print(f"米国株：{run.rows}件（基準日 {as_of}）")
        except Exception as e:  # 失敗も記録する（画面の状態表示に使う）
            s.rollback()
            s.add(IngestionRun(dataset="securities", source="load_securities", status="failed",
                               finished_at=datetime.now(timezone.utc), message=str(e)[:1000]))
            s.commit()
            raise


if __name__ == "__main__":
    main()
