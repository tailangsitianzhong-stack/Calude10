# AI STOCK INTELLIGENCE

日本株・米国株の分析・評価・予測検証Webアプリ（開発中）。設計は `docs/phase1_design.md`。

## 現在の状態：フェーズ3（金融データ連携）完了 — 株価APIのキー待ち

| 機能 | 状態 |
|---|---|
| 画面（ダッシュボード・検索・銘柄詳細・データの状態）、ダークモード、PC・スマートフォン | 実装済み |
| 銘柄マスタ（JPX・Nasdaq Trader） | 実装済み（実データ） |
| 経済指標・金利・為替・株価指数の終値（FRED、26系列） | **実装済み（実データ）** |
| 米国企業の財務データ（SEC EDGAR、時点つき） | **実装済み（実データ、大型株約150社＋指定銘柄）** |
| データ品質の検証（欠損・古さ・異常値）と、取得日時・遅延・信頼性の表示 | 実装済み |
| 日本株の株価・TOPIX（J-Quants API V2） | **実装済み・APIキー待ち**（未設定の間は「未接続」） |
| 米国株の株価（Tiingo / Massive、切り替え可能） | **実装済み・APIキー待ち** |
| 定期取得ワーカー（Docker の worker サービス） | 実装済み |
| 日本企業の財務・ニュース・開示 | 未実装 |
| 採点・ランキング（フェーズ4）、予測（フェーズ5）、検証（フェーズ6）、ログイン（フェーズ7） | 未実装 |

データソースの比較・接続手順は `docs/phase3_data_sources.md`。

## 起動方法（Docker）

```bash
cp .env.example .env            # POSTGRES_PASSWORD などを設定
docker compose up -d --build
# 銘柄マスタを公式の一覧から取り込む（インターネット接続が必要）
docker compose run --rm api python -m scripts.load_securities
# 公開データをすぐに取得（以後は worker が定期的に取得）
docker compose run --rm worker python -m app.worker fred
docker compose run --rm worker python -m app.worker sec
```
- 画面：http://127.0.0.1:3000 　API：http://127.0.0.1:8000/docs
- 社内プロキシ等で追加のCA証明書が必要な環境では、ビルド時に `EXTRA_CA_CERT_FILE=./<証明書ファイル> docker compose up -d --build`（証明書はプロジェクト内に置き、Gitには含めない。イメージにも残らない）。

## 起動方法（Dockerを使わない開発）

```bash
# API（SQLite で動く）
cd backend && python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python -m scripts.load_securities          # または --dir <ダウンロード済みファイルのディレクトリ>
uvicorn app.main:app --port 8000
# Web
cd frontend && npm ci && npm run build && API_BASE_URL=http://127.0.0.1:8000 npm start
```

## テスト

```bash
cd backend && python -m pytest -q                 # 単体テスト（38件）
cd frontend && npx tsc --noEmit && npx next build  # 型検査・ビルド
cd frontend && npm run e2e                         # 画面のE2E（PC・スマートフォン、21件。API・Webの起動が必要）
API_DOWN=1 npm run e2e                             # APIを止めた状態のE2E（エラー表示・数値を作らないこと）
```

## データベースのパスワードの変更

```bash
./scripts/rotate-db-password.sh
```
新しいランダムなパスワードを生成して `.env` を書き換え、データベース側の利用者のパスワードも変更し、
新しいパスワードで接続でき、古いパスワードでは接続できないことを確認してから api・worker を起動し直す。
パスワードの値は画面にもログにも出さない。PostgreSQL は初回起動時にだけ `POSTGRES_PASSWORD` を使うため、
`.env` を手で書き換えるだけでは変更されない点に注意。

## 設計上の約束（変更しない）
- 架空の株価・企業データを実データのように表示しない。データがなければ「未接続」「データ不足」と表示し、値を出さない。
- 秘密情報（APIキー・DBパスワード）はサーバー側の `.env` だけに置き、ブラウザに送らない。
- 上場廃止になった銘柄も削除しない（生存者バイアス対策）。
- 本サービスは情報提供・学習を目的とした分析ツールであり、投資助言ではない。
