"""E2E 用：テスト用DBに仮の株価を直接入れて、データの状況ページの複合状態を表示させる（本番DBには使わない）。

使い方（backend ディレクトリで。DB は必ずテスト用の複製を指定する）:
    python -m tests.e2e_seed_price_scenario --test-db /path/to/copy.sqlite3

入れる内容（値はテスト用の仮の値。外部APIには接続しない）:
- 日本株：上場中の日本株のうち先頭100銘柄に、提供元の遅延（Freeプラン 84日）＋許容日数を超えて古い株価
  → 「一部取得（100／N 銘柄）・データ遅延あり」
- 米国株：取得対象（財務データのある米国株）すべてに、古い株価（30日前）→ 「古いデータ（全 N 銘柄取得済み）」
本番のDB（backend/data/asi.sqlite3）を指定した場合は、何もせずに終了する。
"""
import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROD = (Path(__file__).resolve().parents[1] / "data" / "asi.sqlite3").resolve()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-db", required=True, help="テスト用DBの複製のパス（本番DBは不可）")
    a = ap.parse_args()
    db = Path(a.test_db).resolve()
    if not db.exists():
        print(f"テスト用DBがありません：{db}", file=sys.stderr)
        return 2
    if db == PROD or (PROD.exists() and db.samefile(PROD)):  # リンクなど別の名前でも本番DBなら拒否する
        print("本番DBは指定できません", file=sys.stderr)
        return 2
    c = sqlite3.connect(db)
    now = datetime.now(timezone.utc).isoformat(sep=" ")
    jp = [i for (i,) in c.execute("select id from securities where market='JP' and is_active=1 order by code limit 100")]
    us = [i for (i,) in c.execute("select distinct f.security_id from financial_facts f join securities s on s.id=f.security_id "
                                    "where s.market='US'")]
    jp_day, us_day = date.today() - timedelta(days=84 + 30), date.today() - timedelta(days=30)
    c.executemany("insert into daily_prices (security_id, trade_date, close, provider, fetched_at) values (?,?,?,?,?)",
                  [(i, jp_day.isoformat(), 1000.0, "jquants", now) for i in jp] +
                  [(i, us_day.isoformat(), 100.0, "tiingo", now) for i in us])
    c.commit()
    print(f"テスト用DBに仮の株価を追加：日本株 {len(jp)} 銘柄（{jp_day}）、米国株 {len(us)} 銘柄（{us_day}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
