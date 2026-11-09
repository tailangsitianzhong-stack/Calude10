"""DB の保存先（DATABASE_URL）の検査のテスト。どのテストも一時フォルダの中だけを使い、作業用の DB は開かない。"""
import os
import subprocess
import sys
from pathlib import Path

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
