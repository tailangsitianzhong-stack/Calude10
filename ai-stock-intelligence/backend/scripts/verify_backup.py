"""バックアップの作成と、復元したDBの確認（全銘柄展開などの大きな書き込みの前に使う）。

どちらのDBも読み取り専用（SQLite の mode=ro）で開くため、本番のDBに書き込まない。

使い方（backend ディレクトリで）:
    python -m scripts.verify_backup backup  <本番DB> <出力先.sqlite3.xz>
        … 本番DBを SQLite のバックアップ機能で複製して xz で圧縮し、SHA-256 を <出力先>.sha256 に書く。
    python -m scripts.verify_backup verify  <本番DB> <バックアップ.sqlite3.xz> <作業ディレクトリ>
        … 1) SHA-256 の照合 2) 作業ディレクトリに展開 3) PRAGMA integrity_check
          4) 全テーブルの件数と内容のハッシュを本番と比較 5) 財務APIの応答（財務データのある全銘柄）を本番と比較。
          すべて一致したら終了コード 0。展開したDBは最後に削除する。
"""
import hashlib
import json
import lzma
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

CHUNK = 1 << 20


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)


def backup(live: Path, out: Path) -> None:
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        raw = Path(tmp) / "backup.sqlite3"
        src, dst = ro(live), sqlite3.connect(raw)
        src.backup(dst)
        dst.close()
        src.close()
        with open(raw, "rb") as f, lzma.open(out, "wb", preset=6) as z:
            shutil.copyfileobj(f, z, CHUNK)
        raw_sha = sha256(raw)
    digest = sha256(out)
    Path(f"{out}.sha256").write_text(f"{digest}  {out.name}\n{raw_sha}  {out.name.removesuffix('.xz')}\n")
    print(json.dumps({"backup": str(out), "bytes": out.stat().st_size, "sha256_xz": digest, "sha256_sqlite3": raw_sha}))


def table_digest(path: Path) -> dict:
    c = ro(path)
    out = {"integrity_check": c.execute("PRAGMA integrity_check").fetchone()[0]}
    for (t,) in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name").fetchall():
        h, n = hashlib.sha256(), 0
        for r in c.execute(f'select * from "{t}" order by rowid'):
            h.update(repr(r).encode())
            n += 1
        out[t] = [n, h.hexdigest()]
    c.close()
    return out


DUMP = """
import json, os, sqlite3, sys
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
import app.db as dbmod
# 対象のDBを読み取り専用（mode=ro）で開いてアプリに渡す。DATABASE_URL は "sqlite://"（メモリ）にしてあるので、
# アプリの既定のDBやフォルダ（data/・file: など）を作らない。書き込もうとすると SQLite がエラーにする。
path = os.environ["ASI_VERIFY_DB"]
ro = create_engine("sqlite://", poolclass=StaticPool,
                   creator=lambda: sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False))
dbmod.engine = ro
dbmod.SessionLocal.configure(bind=ro)
from fastapi.testclient import TestClient
from app.db import SessionLocal
from app.main import app
from app.models import FinancialFact, Security
with SessionLocal() as s:
    secs = sorted((m, c) for m, c in s.execute(select(Security.market, Security.code).where(
        Security.id.in_(select(FinancialFact.security_id).distinct()))))
c = TestClient(app)  # with を使わない：起動処理（テーブルの作成）を走らせない
out = {f"{m}:{code}": [r.status_code, r.json()] for m, code in secs
       for r in [c.get(f"/api/v1/securities/{m}/{code}/financials")]}
json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False, sort_keys=True, default=str)
"""


def api_dump(db: Path, out: Path, workdir: Path) -> dict:
    backend = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(backend), "DATABASE_URL": "sqlite://", "ASI_VERIFY_DB": str(db.resolve())}
    script = workdir / "dump_api.py"
    script.write_text(DUMP)
    subprocess.run([sys.executable, str(script), str(out)], env=env, cwd=workdir, check=True)
    return json.loads(out.read_text())


def compare_api(pa: dict, pb: dict) -> dict:
    """財務APIの応答（{銘柄: [状態コード, 本文]}）を比べる。エラー応答（200以外）は、両方で同じでも不合格にする。"""
    return {"api_companies": {"live": len(pa), "restored": len(pb)},
            "api_errors": sum(v[0] != 200 for v in pb.values()) + sum(v[0] != 200 for v in pa.values()),
            "api_different": sorted(k for k in set(pa) | set(pb) if pa.get(k) != pb.get(k))[:20],
            "api_identical": pa == pb}


def verify(live: Path, backup_xz: Path, workdir: Path, dump=None) -> int:
    """dump は財務APIの応答を取り出す関数（テストで差し替える）。既定は api_dump（別のプロセスで読み取り専用に実行）。"""
    dump = dump or api_dump
    workdir.mkdir(parents=True, exist_ok=True)
    result, ok = {}, False  # 途中で例外になった場合は合格にしない
    expect = dict(line.split("  ")[::-1] for line in Path(f"{backup_xz}.sha256").read_text().split("\n") if line)
    result["sha256_xz"] = sha256(backup_xz) == expect[backup_xz.name]
    restored = workdir / "restored.sqlite3"
    try:
        with lzma.open(backup_xz, "rb") as z, open(restored, "wb") as f:
            shutil.copyfileobj(z, f, CHUNK)
        result["sha256_sqlite3"] = sha256(restored) == expect[backup_xz.name.removesuffix(".xz")]
        a, b = table_digest(live), table_digest(restored)
        result["integrity_check"] = b["integrity_check"]
        result["tables"] = {t: v[0] for t, v in b.items() if t != "integrity_check"}
        result["tables_identical"] = a == b
        pa, pb = dump(live, workdir / "api_live.json", workdir), dump(restored, workdir / "api_restored.json", workdir)
        result.update(compare_api(pa, pb))
        ok = (result["sha256_xz"] and result["sha256_sqlite3"] and result["integrity_check"] == "ok"
              and result["tables_identical"] and result["api_identical"] and result["api_errors"] == 0)
    finally:
        restored.unlink(missing_ok=True)
    result["all_ok"] = bool(ok)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "backup":
        backup(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) == 5 and sys.argv[1] == "verify":
        sys.exit(verify(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])))
    else:
        sys.exit(__doc__)
