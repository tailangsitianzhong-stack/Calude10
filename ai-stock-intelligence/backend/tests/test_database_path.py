"""DB の保存先（DATABASE_URL）の検査のテスト。どのテストも一時フォルダの中だけを使い、作業用の DB は開かない。"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

from app import config
from app import db as dbmod
from app import worker

from .conftest import UNUSABLE_DB, WORK_DB

BACKEND = Path(__file__).resolve().parents[1]


def _new_db(path: Path) -> str:
    """テスト専用の DB を明示的に作る（create=True）。"""
    url = f"sqlite:///{path}"
    dbmod.init_db(dbmod.make_engine(url, create=True))
    return url


@pytest.fixture()
def fresh(monkeypatch):
    """アプリのエンジンを未作成の状態にし、テストの後に元へ戻す。"""
    monkeypatch.setattr(dbmod, "engine", None)
    bind = dbmod.SessionLocal.kw.get("bind")
    config.get_settings.cache_clear()
    yield monkeypatch
    dbmod.SessionLocal.configure(bind=bind)
    config.get_settings.cache_clear()


# 1. 絶対パスの SQLite は受け付ける
def test_absolute_sqlite_url_is_accepted(tmp_path):
    url = _new_db(tmp_path / "a.sqlite3")
    assert dbmod.validate_url(url) == url
    assert str(dbmod.make_engine(url).url).endswith("a.sqlite3")


# 2. 相対パスは拒否する（ファイルもフォルダも作らない）
@pytest.mark.parametrize("url", ["sqlite:///./data/asi.sqlite3", "sqlite:///data/asi.sqlite3", "sqlite:///asi.sqlite3"])
def test_relative_sqlite_url_is_rejected(tmp_path, monkeypatch, url):
    monkeypatch.chdir(tmp_path)
    for create in (False, True):
        with pytest.raises(dbmod.DatabaseConfigError, match="相対パス"):
            dbmod.make_engine(url, create=create)
    assert list(tmp_path.iterdir()) == []


# 3. DATABASE_URL が未設定・空なら止まり、DB を作らない（API の起動・ワーカーとも）
def test_unset_database_url_stops_without_creating_anything(tmp_path, fresh):
    fresh.chdir(tmp_path)
    fresh.delenv("DATABASE_URL", raising=False)
    fresh.setattr(config, "ENV_FILES", ())
    fresh.setattr(config.Settings, "model_config", {**config.Settings.model_config, "env_file": None})
    config.get_settings.cache_clear()
    assert config.get_settings().database_url == ""
    with pytest.raises(dbmod.DatabaseConfigError, match="未設定"):
        dbmod.get_engine()
    from fastapi.testclient import TestClient
    from app.main import app
    with pytest.raises(dbmod.DatabaseConfigError, match="未設定"):
        with TestClient(app):
            pass
    fresh.setattr(sys, "argv", ["worker", "fred"])
    ran = []
    fresh.setattr(worker, "job", lambda name, **kw: ran.append(name))
    with pytest.raises(SystemExit) as e:
        worker.main()
    assert e.value.code == 2 and ran == []
    assert list(tmp_path.iterdir()) == [] and dbmod.engine is None


def test_empty_and_invalid_urls_are_rejected():
    for url in ("", "   ", None):
        with pytest.raises(dbmod.DatabaseConfigError, match="未設定"):
            dbmod.validate_url(url)
    with pytest.raises(dbmod.DatabaseConfigError, match="形式が不正"):
        dbmod.validate_url("not a url")
    with pytest.raises(dbmod.DatabaseConfigError, match="URI 形式"):
        dbmod.validate_url("sqlite:///file:/tmp/x.sqlite3?mode=ro&uri=true")
    with pytest.raises(dbmod.DatabaseConfigError, match="接続オプション"):
        dbmod.validate_url("sqlite:////tmp/x.sqlite3?mode=rwc")


# ---- 修正1：実在のファイルを指さない SQLite の URL を拒否する ----
@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///", "sqlite:///:memory:", "sqlite:///   "])
def test_memory_and_empty_sqlite_urls_are_rejected(url, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for create in (False, True):
        with pytest.raises(dbmod.DatabaseConfigError, match="ファイルが指定されていません"):
            dbmod.make_engine(url, create=create)
    assert list(tmp_path.iterdir()) == []


def test_memory_url_stops_api_and_worker(tmp_path, fresh):
    fresh.chdir(tmp_path)
    fresh.setenv("DATABASE_URL", "sqlite://")
    config.get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import app
    with pytest.raises(dbmod.DatabaseConfigError, match="ファイルが指定されていません"):
        with TestClient(app):
            pass
    ran = []
    fresh.setattr(worker, "job", lambda name, **kw: ran.append(name))
    fresh.setattr(sys, "argv", ["worker", "fred"])
    with pytest.raises(SystemExit) as e:
        worker.main()
    assert e.value.code == 2 and ran == [] and dbmod.engine is None
    assert worker.init_database(create_new_sqlite=True) == 2
    assert list(tmp_path.iterdir()) == []


# ---- 修正2：通常の接続は mode=rw（ファイルがなければエラー。作らない） ----
def test_existing_database_opens_for_normal_read_and_write(tmp_path):
    url = _new_db(tmp_path / "rw.sqlite3")
    eng = dbmod.make_engine(url)  # 通常の接続（create=False）
    with eng.begin() as c:
        c.exec_driver_sql("create table t(x)")
        c.exec_driver_sql("insert into t values (1)")
    with eng.connect() as c:
        assert c.exec_driver_sql("select x from t").scalar() == 1
    eng.dispose()


def test_normal_connection_to_missing_file_fails_and_creates_nothing(tmp_path):
    p = tmp_path / "missing.sqlite3"
    with pytest.raises(dbmod.DatabaseConfigError, match="存在しません"):
        dbmod.make_engine(f"sqlite:///{p}")
    with pytest.raises(sqlite3.OperationalError):  # 起動時の検査を通さずに、接続の方式そのものでも作らない
        sqlite3.connect(dbmod.sqlite_uri(p), uri=True)
    assert list(tmp_path.iterdir()) == []


def test_file_removed_after_engine_creation_is_not_recreated(tmp_path):
    url = _new_db(tmp_path / "gone.sqlite3")
    eng = dbmod.make_engine(url)
    with eng.connect() as c:
        c.exec_driver_sql("select 1")
    (tmp_path / "gone.sqlite3").unlink()  # 動作中に DB ファイルが消えた・移動した
    eng.dispose()  # 接続を捨てて、新しく接続し直させる
    from sqlalchemy.exc import OperationalError
    with pytest.raises(OperationalError):
        with eng.connect() as c:
            c.exec_driver_sql("select 1")
    assert list(tmp_path.iterdir()) == []  # 空の DB（や付属ファイル）を作っていない


def test_explicit_create_still_creates_a_test_database(tmp_path):
    p = tmp_path / "created.sqlite3"
    eng = dbmod.make_engine(f"sqlite:///{p}", create=True)
    assert not p.exists()  # エンジンを作っただけではまだ作らない
    dbmod.init_db(eng)
    assert p.is_file()
    dbmod.make_engine(f"sqlite:///{p}").dispose()  # 作ったあとは通常の接続で開ける


def test_init_db_result_opens_with_normal_connection(tmp_path, fresh):
    p = tmp_path / "setup.sqlite3"
    fresh.setenv("DATABASE_URL", f"sqlite:///{p}")
    config.get_settings.cache_clear()
    assert worker.init_database(create_new_sqlite=True) == 0
    fresh.setattr(dbmod, "engine", None)
    with dbmod.get_engine().connect() as c:  # 通常の起動の経路（mode=rw）
        assert c.exec_driver_sql("select count(*) from sqlite_master where name='securities'").scalar() == 1


@pytest.mark.parametrize("name", ["空白 と 日本語", "a#b%c", "100%_test", "x[1];y=2&z"])
def test_paths_with_spaces_japanese_and_special_characters(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    p = folder / "データ #1.sqlite3"
    dbmod.init_db(dbmod.make_engine(f"sqlite:///{p}", create=True))
    # 指定したとおりの場所に1つだけ作られている（文字の解釈の違いで別の場所に作っていない）
    assert p.is_file() and sorted(str(x.relative_to(tmp_path)) for x in tmp_path.rglob("*")) == [name, f"{name}/データ #1.sqlite3"]
    eng = dbmod.make_engine(f"sqlite:///{p}")
    with eng.connect() as c:
        assert c.exec_driver_sql("select count(*) from sqlite_master").scalar() > 0
    eng.dispose()


def test_question_mark_in_sqlite_path_is_rejected(tmp_path):
    """SQLAlchemy は「?」以降をパスから切り捨てる（例 /t/a?b/x.db → /t/a）。別のファイルを指さないよう拒否する。"""
    d = tmp_path / "a?b"
    d.mkdir()
    for create in (False, True):
        with pytest.raises(dbmod.DatabaseConfigError, match="「\\?」を含める"):
            dbmod.make_engine(f"sqlite:///{d / 'x.sqlite3'}", create=create)
    assert sorted(x.name for x in tmp_path.iterdir()) == ["a?b"] and list(d.iterdir()) == []


def test_sqlite_uri_format_for_posix_and_windows_paths():
    assert dbmod.sqlite_uri(Path("/srv/asi/data/asi.sqlite3")) == "file:/srv/asi/data/asi.sqlite3?mode=rw"
    assert dbmod.sqlite_uri(Path("/srv/asi/data/asi.sqlite3"), create=True) == "file:/srv/asi/data/asi.sqlite3?mode=rwc"
    assert dbmod.sqlite_uri(Path("/a b/x#?.db")) == "file:/a%20b/x%23%3F.db?mode=rw"
    # Windows のドライブ文字：file:/D:/… とする（D: をスキームとして扱わない）。実機の Windows では未検証
    assert dbmod.sqlite_uri(PureWindowsPath(r"D:\ASI\data\asi.sqlite3")) == "file:/D:/ASI/data/asi.sqlite3?mode=rw"


@pytest.mark.skipif(os.name == "nt", reason="Windows では D:/… は絶対パス")
def test_windows_drive_path_is_rejected_on_linux():
    with pytest.raises(dbmod.DatabaseConfigError, match="相対パス"):
        dbmod.validate_url("sqlite:///D:/ASI/data/asi.sqlite3")


# 4. 存在しない保存先・フォルダ・ファイルでないもの
def test_missing_database_file_is_not_created(tmp_path):
    p = tmp_path / "missing.sqlite3"
    with pytest.raises(dbmod.DatabaseConfigError, match="存在しません"):
        dbmod.make_engine(f"sqlite:///{p}")
    assert not p.exists()


def test_missing_directory_is_not_created_even_for_setup(tmp_path):
    p = tmp_path / "no-such-dir" / "asi.sqlite3"
    for create in (False, True):
        with pytest.raises(dbmod.DatabaseConfigError, match="フォルダが存在しません"):
            dbmod.make_engine(f"sqlite:///{p}", create=create)
    assert not p.parent.exists()


def test_directory_instead_of_file_is_rejected(tmp_path):
    with pytest.raises(dbmod.DatabaseConfigError, match="ファイルではありません"):
        dbmod.validate_url(f"sqlite:///{tmp_path}")


@pytest.mark.skipif(os.geteuid() == 0, reason="root は権限に関係なく読み書きできるため")
def test_unreadable_database_is_rejected(tmp_path):
    p = tmp_path / "locked.sqlite3"
    p.write_bytes(b"")
    p.chmod(0o000)
    with pytest.raises(dbmod.DatabaseConfigError, match="読み書きできません"):
        dbmod.validate_url(f"sqlite:///{p}")


def test_postgres_url_is_passed_through_and_password_hidden():
    url = "postgresql+psycopg://asi:secret-pw@db:5432/asi"
    assert dbmod.validate_url(url) == url
    ident = dbmod.db_identity(url)
    assert ident["backend"] == "postgresql" and ident["host"] == "db" and ident["database"] == "asi"
    assert "secret-pw" not in repr(ident)


# 初回セットアップ：init-db は明示の指定があるときだけ新しい DB を作り、既存の DB には何もしない
def test_init_db_creates_only_with_explicit_flag_and_never_touches_existing(tmp_path, fresh):
    p = tmp_path / "new.sqlite3"
    fresh.setenv("DATABASE_URL", f"sqlite:///{p}")
    config.get_settings.cache_clear()
    assert worker.init_database(create_new_sqlite=False) == 2 and not p.exists()
    assert worker.init_database(create_new_sqlite=True) == 0 and p.exists()
    before = (p.stat().st_size, p.stat().st_mtime_ns)
    fresh.setattr(dbmod, "engine", None)
    assert worker.init_database(create_new_sqlite=True) == 2  # すでにある：上書き・初期化しない
    assert (p.stat().st_size, p.stat().st_mtime_ns) == before


def test_init_db_refuses_relative_path(tmp_path, fresh):
    fresh.chdir(tmp_path)
    fresh.setenv("DATABASE_URL", "sqlite:///./data/asi.sqlite3")
    config.get_settings.cache_clear()
    assert worker.init_database(create_new_sqlite=True) == 2
    assert list(tmp_path.iterdir()) == []


# 5・6. API とワーカーが同じ DB を使っているかを比べる（同じなら 0、違えば 2）
def _api_fingerprint_via_testclient(url: str, fresh) -> str:
    from fastapi.testclient import TestClient
    from app.main import app
    fresh.setenv("DATABASE_URL", url)
    config.get_settings.cache_clear()
    fresh.setattr(dbmod, "engine", None)
    with TestClient(app) as c:
        fp = c.get("/healthz").json()["db"]["fingerprint"]
    return fp


def test_api_and_worker_use_the_same_database(tmp_path, fresh):
    url = _new_db(tmp_path / "shared.sqlite3")
    api_fp = _api_fingerprint_via_testclient(url, fresh)
    fresh.setattr(dbmod, "engine", None)  # ワーカーとして、同じ設定から作り直す
    fresh.setattr(worker, "api_db_fingerprint", lambda u, attempts=3: api_fp)
    assert worker.check_db("http://api.invalid") == 0


def test_different_databases_are_detected(tmp_path, fresh):
    api_fp = _api_fingerprint_via_testclient(_new_db(tmp_path / "api.sqlite3"), fresh)
    other = _new_db(tmp_path / "worker.sqlite3")
    fresh.setenv("DATABASE_URL", other)
    config.get_settings.cache_clear()
    fresh.setattr(dbmod, "engine", None)
    fresh.setattr(worker, "api_db_fingerprint", lambda u, attempts=3: api_fp)
    assert worker.check_db("http://api.invalid") == 2
    # 起動時の確認（DB_CHECK_API_URL）でも止まり、取得の処理は始まらない
    ran = []
    fresh.setattr(worker, "job", lambda name, **kw: ran.append(name))
    fresh.setenv("DB_CHECK_API_URL", "http://api.invalid")
    fresh.setattr(sys, "argv", ["worker", "fred"])
    fresh.setattr(dbmod, "engine", None)
    with pytest.raises(SystemExit) as e:
        worker.main()
    assert e.value.code == 2 and ran == []


def test_unreachable_api_is_not_treated_as_same(tmp_path, fresh):
    fresh.setenv("DATABASE_URL", _new_db(tmp_path / "x.sqlite3"))
    config.get_settings.cache_clear()
    fresh.setattr(worker, "api_db_fingerprint", lambda u, attempts=3: None)
    assert worker.check_db("http://api.invalid") == 2


def test_symlinked_path_has_the_same_fingerprint(tmp_path):
    url = _new_db(tmp_path / "real.sqlite3")
    (tmp_path / "link.sqlite3").symlink_to(tmp_path / "real.sqlite3")
    assert dbmod.db_identity(url)["fingerprint"] == dbmod.db_identity(f"sqlite:///{tmp_path / 'link.sqlite3'}")["fingerprint"]


# 7. テスト専用の DB で通常の処理（API の起動・状態の取得）が動く
def test_normal_processing_on_a_test_database(client):
    r = client.get("/api/v1/system/status")
    assert r.status_code == 200
    p = dbmod.sqlite_path(dbmod.engine.url.render_as_string(hide_password=False))
    assert p is not None and p.is_absolute() and p.exists()


# 8. テストは作業用の DB を参照しない
def test_tests_never_point_at_the_work_database(client):
    p = Path(os.path.realpath(dbmod.sqlite_path(dbmod.engine.url.render_as_string(hide_password=False))))
    assert p != Path(os.path.realpath(WORK_DB))
    assert Path(UNUSABLE_DB).is_absolute() and not Path(UNUSABLE_DB).exists()
    assert os.path.realpath(Path(UNUSABLE_DB).parent) != os.path.realpath(WORK_DB.parent)


# 9. モジュールを読み込んだだけでは、エンジンもファイルも作らない（別のプロセスで確かめる）
@pytest.mark.parametrize("env_url", [None, "relative", "missing"])
def test_import_creates_no_engine_and_no_file(tmp_path, env_url):
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    if env_url == "relative":
        env["DATABASE_URL"] = "sqlite:///./data/asi.sqlite3"
    elif env_url == "missing":
        env["DATABASE_URL"] = f"sqlite:///{tmp_path / 'missing.sqlite3'}"
    env["PYTHONPATH"] = str(BACKEND)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    work = tmp_path / "cwd"
    work.mkdir()
    code = ("import app.db, app.models, app.main, app.worker; import app.db as d; "
            "print('engine-none' if d.engine is None and d.SessionLocal.kw.get('bind') is None else 'engine-created')")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=work, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "engine-none"
    assert list(work.iterdir()) == [] and not (tmp_path / "missing.sqlite3").exists()
