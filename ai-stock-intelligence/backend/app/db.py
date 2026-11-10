"""データベース接続。本番は PostgreSQL、開発・テストは SQLite でも動く。

接続先は環境変数 DATABASE_URL だけで決める（API・ワーカー・スクリプトで共通。設定は config.Settings）。
- 未設定・空・不正な値なら起動しない（既定の SQLite に黙って切り替えない）
- SQLite は絶対パスだけを受け付ける（相対パスは、起動したディレクトリによって別のファイルを指すため拒否）
- メモリ上の DB・ファイル名が空の SQLite（sqlite://・sqlite:///・sqlite:///:memory:）は受け付けない
- SQLite のファイルとフォルダは自動では作らない。新しいDBを作るのは、初回セットアップの
  `python -m app.worker init-db --create-new-sqlite` と、テストが明示的に create=True を指定したときだけ
- 通常の接続は SQLite の URI の mode=rw で開く（ファイルがなければ接続がエラーになり、作らない）。
  起動時の検査だけでなく、起動後に DB ファイルが消えた・移動した場合の再接続でも空の DB を作らない
- エンジンは最初に使うときに作る（このモジュールを読み込んだだけでは、どのDBにも接続しない）
"""
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path, PurePath
from urllib.parse import quote

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


class DatabaseConfigError(RuntimeError):
    """DATABASE_URL の設定の誤り（未設定・相対パス・存在しない保存先など）。メッセージに原因と直し方を書く。"""


HELP = ("DATABASE_URL に保存先を絶対パスで指定してください（例：sqlite:////srv/asi/data/asi.sqlite3、"
        "Windows は sqlite:///D:/ASI/data/asi.sqlite3、PostgreSQL は postgresql+psycopg://…）。API とワーカーは同じ値を使います")


def _redacted(url: str) -> str:
    """エラーや表示に使う URL（パスワードを伏せる）。"""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except ArgumentError:
        return "（解釈できない値）"


def sqlite_path(url: str) -> Path | None:
    """SQLite のファイルの URL ならそのパス（検査はしない）。メモリ上の DB・SQLite 以外なら None。"""
    u = make_url(url)
    if u.get_backend_name() != "sqlite" or u.database in (None, "", ":memory:"):
        return None
    return Path(u.database)


def validate_url(url: str | None, create: bool = False) -> str:
    """DATABASE_URL を検査して、使ってよい URL を返す。問題があれば DatabaseConfigError（DB には接続しない）。

    create=False（API・ワーカーの通常の起動）：SQLite のファイルがすでに存在し、読み書きできること。
    create=True（初回セットアップ・テスト）：ファイルはまだなくてよい。ただしフォルダは存在すること（自動で作らない）。
    """
    url = (url or "").strip()
    if not url:
        raise DatabaseConfigError("DATABASE_URL が未設定です。" + HELP)
    try:
        u = make_url(url)
    except ArgumentError:
        raise DatabaseConfigError("DATABASE_URL の形式が不正です。" + HELP) from None
    if u.get_backend_name() != "sqlite":
        return url  # PostgreSQL など：保存先は DB サーバー側（Docker Compose では名前付きボリューム）
    if u.database is None or u.database.strip() in ("", ":memory:"):
        # sqlite://・sqlite:///・sqlite:///:memory: は実在のファイルを指さない（終了すると消える一時的な DB になる）
        raise DatabaseConfigError("SQLite の DATABASE_URL にデータベースのファイルが指定されていません"
                                  f"（{u.render_as_string(hide_password=True)}）。メモリ上の DB・空のファイル名は使えません。" + HELP)
    if u.database.startswith("file:") or u.query or "?" in url:
        # 「?」より後は接続オプションとして解釈され、パスの一部なら黙って切り捨てられる（別のファイルを指してしまう）
        raise DatabaseConfigError("SQLite の URI 形式（file:…）や接続オプション（?…）は使えません。"
                                  "保存先のパスに「?」を含めることもできません。" + HELP)
    p = Path(u.database)
    if not p.is_absolute():
        raise DatabaseConfigError(f"SQLite の保存先が相対パスです（{u.database}）。起動したフォルダによって別のファイルを"
                                  "指してしまうため使えません。" + HELP)
    if not p.parent.is_dir():
        raise DatabaseConfigError(f"SQLite の保存先のフォルダが存在しません（{p.parent}）。自動では作りません。"
                                  "フォルダを確認してください")
    if p.exists():
        if not p.is_file():
            raise DatabaseConfigError(f"SQLite の保存先がファイルではありません（{p}）")
        if not os.access(p, os.R_OK | os.W_OK):
            raise DatabaseConfigError(f"SQLite の保存先のファイルを読み書きできません（{p}）。権限を確認してください")
    elif not create:
        raise DatabaseConfigError(f"SQLite のデータベースが存在しません（{p}）。新しい空のDBは自動では作りません。"
                                  "保存先が正しいか確認してください。初めて作る場合だけ "
                                  "`python -m app.worker init-db --create-new-sqlite` を実行します")
    elif not os.access(p.parent, os.W_OK):
        raise DatabaseConfigError(f"SQLite の保存先のフォルダに書き込めません（{p.parent}）")
    return url


def sqlite_uri(path: PurePath, create: bool = False) -> str:
    """SQLite の URI（file:…?mode=rw）。create=False なら、ファイルがないときに接続がエラーになる（作らない）。
    パスは / 区切りにして、空白・日本語・#・?・% などをパーセント符号化する（SQLite が元に戻す）。
    Windows のドライブ文字（D:/…）は先頭に / を付けて file:/D:/… とする（SQLite の URI の規則。D: をスキームなどと
    取り違えない）。"""
    p = path.as_posix()
    if not p.startswith("/"):
        p = "/" + p
    return "file:" + quote(p, safe="/:") + ("?mode=rwc" if create else "?mode=rw")


def make_engine(url: str | None = None, create: bool = False):
    """検査済みの URL でエンジンを作る。url を省略すると設定（DATABASE_URL）を使う。

    SQLite は、エンジンの URL は通常のパス（sqlite:///絶対パス。接続の管理方式・識別情報はこれで決まる）のまま、
    実際の接続は creator で URI の mode=rw（create=True のときだけ mode=rwc）を指定して開く。"""
    url = validate_url(url if url is not None else get_settings().database_url, create=create)
    if make_url(url).get_backend_name() == "sqlite":
        uri = sqlite_uri(Path(make_url(url).database), create=create)
        return create_engine(url, creator=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False))
    return create_engine(url, pool_pre_ping=True)


engine = None  # 最初に get_engine() を呼んだときに作る（読み込んだだけでは作らない）
SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)


def get_engine():
    """アプリ（API・ワーカー・スクリプト）が使うエンジン。まだなければ設定から作り、SessionLocal に結び付ける。"""
    global engine
    if engine is None:
        engine = make_engine()
    if SessionLocal.kw.get("bind") is not engine:
        SessionLocal.configure(bind=engine)
    return engine


def get_session() -> Iterator[Session]:
    get_engine()
    with SessionLocal() as s:
        yield s


def db_identity(url: str | None = None) -> dict:
    """接続先の識別情報（秘密を含まない）。SQLite は実パスと装置・inode、ほかは方式・ホスト・ポート・DB名・利用者名。
    fingerprint は API とワーカーが同じ DB を使っているかを比べるための短いハッシュ。"""
    if url is None:
        url = str(engine.url.render_as_string(hide_password=False)) if engine is not None else get_settings().database_url
    u = make_url(url)
    if u.get_backend_name() == "sqlite":
        p = sqlite_path(url)
        if p is None:
            ident = {"backend": "sqlite", "memory": True}
        else:
            real = Path(os.path.realpath(p))
            st = real.stat() if real.exists() else None
            ident = {"backend": "sqlite", "path": str(real), "device": st.st_dev if st else None, "inode": st.st_ino if st else None}
    else:
        ident = {"backend": u.get_backend_name(), "host": u.host, "port": u.port, "database": u.database, "username": u.username}
    ident["fingerprint"] = hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()[:16]
    return ident


# 既存のデータベースに後から追加した列（簡易マイグレーション）。本格的な移行管理はフェーズ7で Alembic に切り替える
ADDED_COLUMNS = {"securities": {"cik": "INTEGER"}}


def init_db(bind=None) -> None:
    from sqlalchemy import inspect, text

    from . import models  # noqa: F401  モデルを登録する

    eng = bind or get_engine()
    Base.metadata.create_all(bind=eng)
    insp = inspect(eng)
    with eng.begin() as conn:
        for table, cols in ADDED_COLUMNS.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in cols.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
