"""テスト用の設定。テストは一時的な SQLite を使い、本番のデータベースに触れない。

- このファイルの読み込み時（アプリのモジュールを読み込む前）に、DATABASE_URL を一時フォルダの中の
  「まだ存在しない」DB の絶対パスにする。テストの準備（client など）を通さずに DB を使おうとすると、
  存在しない DB として止まる（作業用の DB を開くことも、新しい DB を黙って作ることもない）
- テストの DB は tmp_path（pytest の一時フォルダ）の中に make_engine(…, create=True) で明示的に作る
- 各テストの後に、使っていたエンジンが作業用の DB（backend/data/asi.sqlite3）でないことを確かめる

テストで使う銘柄一覧は、実際の一覧の書式を再現した小さなファイルをテストの中で作る（画面には出ない）。
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

_SESSION_TMP = tempfile.mkdtemp(prefix="asi-test-")
atexit.register(shutil.rmtree, _SESSION_TMP, True)
UNUSABLE_DB = os.path.join(_SESSION_TMP, "not-created.sqlite3")  # 存在しない（テストの準備なしに使うと止まる）
os.environ["DATABASE_URL"] = f"sqlite:///{UNUSABLE_DB}"
WORK_DB = (Path(__file__).resolve().parents[1] / "data" / "asi.sqlite3")  # 作業用の DB の場所（開かない）

import openpyxl
import pytest


@pytest.fixture(autouse=True)
def _never_the_work_db():
    """テストの前後で、アプリのエンジンが作業用の DB を指していないことを確かめる（パスの比較だけ。DB は開かない）。"""
    from app import db as dbmod

    def check():
        if dbmod.engine is not None:
            p = dbmod.sqlite_path(dbmod.engine.url.render_as_string(hide_password=False))
            assert p is None or os.path.realpath(p) != os.path.realpath(WORK_DB), "テストが作業用の DB を指している"
    check()
    yield
    check()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "test.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    # 手元の .env に本物のキーが入っていても、テストはキー未設定の状態で行う（環境変数が .env より優先される）
    for k in ("JQUANTS_API_KEY", "JQUANTS_REFRESH_TOKEN", "US_PRICE_PROVIDER", "TIINGO_API_KEY", "MASSIVE_API_KEY",
              "POLYGON_API_KEY", "SEC_USER_AGENT", "FRED_API_KEY", "EDINET_API_KEY", "ANTHROPIC_API_KEY", "NETWORK_SECRETS"):
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("JQUANTS_PLAN", "free")
    from app import config, db as dbmod
    config.get_settings.cache_clear()
    engine = dbmod.make_engine(f"sqlite:///{db}", create=True)
    dbmod.engine = engine
    dbmod.SessionLocal.configure(bind=engine)
    dbmod.init_db(engine)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def raw_files(tmp_path) -> Path:
    d = tmp_path / "raw"
    d.mkdir()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["日付", "コード", "銘柄名", "市場・商品区分", "33業種コード", "33業種区分", "17業種コード", "17業種区分", "規模コード", "規模区分"])
    ws.append([20260930, 7203, "トヨタ自動車", "プライム（内国株式）", 3700, "輸送用機器", 6, "自動車・輸送機", 1, "TOPIX Core30"])
    ws.append([20260930, 8306, "三菱ＵＦＪフィナンシャル・グループ", "プライム（内国株式）", 7050, "銀行業", 15, "銀行", 1, "TOPIX Core30"])
    ws.append([20260930, 1306, "ＮＥＸＴ　ＦＵＮＤＳ　ＴＯＰＩＸ連動型上場投信", "ETF・ETN", "-", "-", "-", "-", "-", "-"])
    ws.append([20260930, "130A", "テスト新コード", "グロース（内国株式）", 5250, "情報・通信業", 10, "情報通信・サービスその他", 7, "-"])
    wb.save(d / "data_j.xlsx")
    (d / "nasdaqlisted.txt").write_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\n"
        "NVDA|NVIDIA Corporation - Common Stock|Q|N|N|100|N|N\n"
        "QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N\n"
        "ZZZT|Test Issue Inc|Q|Y|N|100|N|N\n"
        "File Creation Time: 1103202618:01|||||||\n", encoding="utf-8")
    (d / "otherlisted.txt").write_text(
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        "KO|Coca-Cola Company (The) Common Stock|N|KO|N|100|N|KO\n"
        "JPM|JP Morgan Chase & Co. Common Stock|N|JPM|N|100|N|JPM\n"
        "JPM$C|JP Morgan Chase & Co. Depositary Shares Series C Preferred|N|JPMpC|N|100|N|JPM-C\n"
        "File Creation Time: 1103202618:01|||||||\n", encoding="utf-8")
    return d
