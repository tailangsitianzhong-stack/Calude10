"""バックアップの作成と確認のスクリプト（scripts/verify_backup.py）のテスト。元のDBには書き込まない。"""
import sqlite3

from scripts import verify_backup as V


def test_backup_hashes_and_table_digest_match_and_source_untouched(tmp_path):
    live = tmp_path / "live.sqlite3"
    c = sqlite3.connect(live)
    c.execute("create table t (id integer primary key, v text)")
    c.executemany("insert into t (v) values (?)", [("a",), ("b",)])
    c.commit()
    c.close()
    before = V.sha256(live)
    out = tmp_path / "b.sqlite3.xz"
    V.backup(live, out)
    assert V.sha256(live) == before  # 元のDBは変えない
    lines = (tmp_path / "b.sqlite3.xz.sha256").read_text().splitlines()
    assert lines[0] == f"{V.sha256(out)}  b.sqlite3.xz"
    import lzma
    restored = tmp_path / "r.sqlite3"
    restored.write_bytes(lzma.open(out).read())
    assert lines[1] == f"{V.sha256(restored)}  b.sqlite3"
    a, b = V.table_digest(live), V.table_digest(restored)
    assert a == b and b["integrity_check"] == "ok" and b["t"][0] == 2
    c = sqlite3.connect(restored)
    c.execute("update t set v='x' where id=1")
    c.commit()
    c.close()
    assert V.table_digest(restored) != a  # 内容が違えば一致しない
