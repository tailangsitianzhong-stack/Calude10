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


# ---- verify()：復元・整合性・財務APIの比較（m6） ----

def _app_db(path):
    """アプリと同じ構造の小さなDB（米国株1社・財務データ2件）を作る。"""
    from datetime import date, datetime, timezone
    from sqlalchemy.orm import Session
    from app.db import init_db, make_engine
    from app.models import FinancialFact, Security
    eng = make_engine(f"sqlite:///{path}", create=True)
    init_db(eng)
    with Session(eng) as s:
        x = Security(market="US", code="AAA", cik=1, name="AAA Inc", name_search="aaa", security_type="common",
                     is_active=True, source="t", fetched_at=datetime.now(timezone.utc))
        s.add(x)
        s.flush()
        for concept, tag, v in (("revenue", "Revenues", 1000.0), ("net_income", "NetIncomeLoss", 100.0)):
            s.add(FinancialFact(security_id=x.id, concept=concept, tag=tag, unit="USD", period_start=date(2025, 1, 1),
                                period_end=date(2025, 12, 31), form="10-K", accn="a", filed=date(2026, 2, 1), value=v,
                                source="SEC EDGAR XBRL", fetched_at=datetime.now(timezone.utc)))
        s.commit()
    eng.dispose()


def _verify(tmp_path, live, xz, capsys, dump=None):
    work = tmp_path / "work"
    code = V.verify(live, xz, work, dump=dump)
    import json
    return code, json.loads(capsys.readouterr().out), work


def test_verify_all_ok_without_writing_or_creating_folders(tmp_path, capsys):
    live = tmp_path / "live.sqlite3"
    _app_db(live)
    xz = tmp_path / "b.sqlite3.xz"
    V.backup(live, xz)
    capsys.readouterr()
    before = V.sha256(live)
    code, res, work = _verify(tmp_path, live, xz, capsys)
    assert code == 0 and res["all_ok"] and res["integrity_check"] == "ok" and res["tables_identical"]
    assert res["api_identical"] and res["api_errors"] == 0 and res["api_companies"] == {"live": 1, "restored": 1}
    assert V.sha256(live) == before  # 本番側のDBに書き込まない
    # 作業ディレクトリには、スクリプトと応答のファイルだけ（復元したDBは削除、file:・data などのフォルダを作らない）
    assert sorted(p.name for p in work.iterdir()) == ["api_live.json", "api_restored.json", "dump_api.py"]
    assert not [p for p in tmp_path.rglob("*") if p.is_dir() and p.name not in ("work",)]


def test_verify_detects_api_and_table_differences(tmp_path, capsys):
    live = tmp_path / "live.sqlite3"
    _app_db(live)
    xz = tmp_path / "b.sqlite3.xz"
    V.backup(live, xz)
    capsys.readouterr()
    c = sqlite3.connect(live)  # バックアップのあとで本番側の値が変わった場合
    c.execute("update financial_facts set value = 2000 where concept = 'revenue'")
    c.commit()
    c.close()
    code, res, _ = _verify(tmp_path, live, xz, capsys)
    assert code == 1 and not res["all_ok"] and res["sha256_xz"] and res["sha256_sqlite3"] and res["integrity_check"] == "ok"
    assert not res["tables_identical"] and not res["api_identical"] and res["api_different"] == ["US:AAA"]


def test_verify_fails_on_api_errors_even_if_identical(tmp_path, capsys):
    live = tmp_path / "live.sqlite3"
    _app_db(live)
    xz = tmp_path / "b.sqlite3.xz"
    V.backup(live, xz)
    capsys.readouterr()
    err = lambda db, out, work: {"US:AAA": [500, {"detail": "Internal Server Error"}]}  # 両方とも同じエラー応答
    code, res, _ = _verify(tmp_path, live, xz, capsys, dump=err)
    assert code == 1 and not res["all_ok"] and res["api_identical"] and res["api_errors"] == 2


def test_verify_fails_on_corrupted_backup(tmp_path, capsys):
    live = tmp_path / "live.sqlite3"
    _app_db(live)
    xz = tmp_path / "b.sqlite3.xz"
    V.backup(live, xz)
    capsys.readouterr()
    sums = (tmp_path / "b.sqlite3.xz.sha256").read_text().replace(V.sha256(xz), "0" * 64)
    (tmp_path / "b.sqlite3.xz.sha256").write_text(sums)
    code, res, _ = _verify(tmp_path, live, xz, capsys, dump=lambda db, out, work: {})
    assert code == 1 and res["sha256_xz"] is False and not res["all_ok"]
