# データベースの保存先（DATABASE_URL）

API・ワーカー・スクリプトは、環境変数 `DATABASE_URL` だけで接続先を決めます（`backend/app/config.py` と `backend/app/db.py`）。
起動したフォルダや既定値によって、別のデータベースを黙って使ったり作ったりしないようにしています。

## 設定の方法

| 使い方 | DATABASE_URL の例 |
|---|---|
| Docker Compose | 設定不要（`docker-compose.yml` が api・worker に同じ PostgreSQL の接続先を設定する。`.env` の値は使われない） |
| SQLite（Linux・macOS） | `sqlite:////srv/asi/data/asi.sqlite3`（`sqlite:` のあとにスラッシュ3つ＋絶対パス） |
| SQLite（Windows） | `sqlite:///D:/ASI/data/asi.sqlite3`（ドライブ文字からの絶対パス） |
| PostgreSQL（Docker なし） | `postgresql+psycopg://asi:パスワード@127.0.0.1:5432/asi` |

- **API とワーカーは同じ値を使います。** `.env`（プロジェクト直下）に1行書けば両方が読みます。
- SQLite は**ローカルのディスク**（SSD など）に置きます。ネットワーク共有や、Dropbox・OneDrive などの同期フォルダの中には置きません。
- 相対パス（`sqlite:///./data/asi.sqlite3`・`sqlite:///data/asi.sqlite3`）は使えません。起動したフォルダによって別のファイルを指すためです。

## 初回セットアップ（新しい空の DB を作る）

```bash
mkdir -p /srv/asi/data                      # フォルダは自動では作らない
export DATABASE_URL=sqlite:////srv/asi/data/asi.sqlite3
python -m app.worker init-db --create-new-sqlite
```

`init-db` は DB がまだないときだけ作ります。すでにある DB には何もしません（上書き・初期化しない）。
バックアップから戻す場合は `init-db` を使わず、戻したファイルの場所を `DATABASE_URL` に指定します。

## 接続先の確認

```bash
python -m app.worker check-db                                  # 検査と識別情報（実パス・装置・inode）の表示。書き込まない
python -m app.worker check-db --api-url http://127.0.0.1:8000  # 動いている API と同じ DB か比べる（違えば終了コード 2）
```

- API の `/healthz` は、使っている DB の識別用の短いハッシュ（`db.fingerprint`）を返します（パス・パスワードは含めない）。
- ワーカーに `DB_CHECK_API_URL`（Docker Compose では `http://api:8000`）を設定すると、起動時に API と比べ、
  違う DB を使っている・確認できないときは取得を始めずに止まります。

## 設定を誤ったときのエラー

| 状態 | 動作 |
|---|---|
| `DATABASE_URL` が未設定・空 | 「DATABASE_URL が未設定です」で止まる（API は起動しない。ワーカーは終了コード 2） |
| SQLite の相対パス | 「SQLite の保存先が相対パスです」で止まる |
| フォルダが存在しない | 「保存先のフォルダが存在しません」で止まる（フォルダを作らない） |
| DB のファイルが存在しない | 「データベースが存在しません」で止まる（新しい空の DB を作らない） |
| ファイルではない・読み書きできない | 原因を示して止まる |
| 形式が不正・`file:` の URI 形式 | 「形式が不正です」「URI 形式は使えません」で止まる |

どの場合も DB への接続の前に止まり、ファイルもフォルダも作りません。

## テスト専用の DB

- `backend/tests/conftest.py` が、読み込みの最初に `DATABASE_URL` を一時フォルダの中の「存在しない」DB の絶対パスにします。
  テストの準備を通さずに DB を使おうとすると止まります。
- テストの DB は pytest の一時フォルダ（`tmp_path`）の中に、`make_engine(url, create=True)` で明示的に作ります（終了後に消える）。
- 各テストの前後で、エンジンが作業用の DB（`backend/data/asi.sqlite3`）を指していないことを確かめます。
- `app.db` を読み込んだだけではエンジンを作りません（最初に `get_engine()` を呼んだときに作る）。
