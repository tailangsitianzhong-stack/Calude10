# AI STOCK INTELLIGENCE

日本株・米国株の分析・評価・予測検証Webアプリ（開発中）。設計は `docs/phase1_design.md`。

## 現在の状態：フェーズ2（Webの基本構築）完了

| 機能 | 状態 |
|---|---|
| ダッシュボード・銘柄検索・銘柄詳細の画面（ダークモード既定／ライト切替、PC・スマートフォン対応） | **実装済み** |
| 銘柄マスタ（JPX 東証上場銘柄一覧、Nasdaq Trader Symbol Directory）の取り込みと検索 | **実装済み**（実データ） |
| データの状態バー・プロバイダーの接続状態の表示 | **実装済み** |
| Docker Compose（PostgreSQL・API・Web） | **実装済み** |
| 株価・指数・財務・ニュースの取得 | **未実装**（フェーズ3）。画面には「未接続」と表示し、値は表示しない |
| 採点・ランキング | **未実装**（フェーズ4） |
| 予測・理論株価・AIコメント | **未実装**（フェーズ5） |
| 予測検証・改ざん防止 | **未実装**（フェーズ6） |
| 1人用ログイン | **未実装**（フェーズ7で有効化予定。現在は 127.0.0.1 にだけ公開） |

## 起動方法（Docker）

```bash
cp .env.example .env            # POSTGRES_PASSWORD などを設定
docker compose up -d --build
# 銘柄マスタを公式の一覧から取り込む（インターネット接続が必要）
docker compose run --rm api python -m scripts.load_securities
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
cd backend && python -m pytest -q                 # APIの単体テスト（6件）
cd frontend && npx tsc --noEmit && npx next build  # 型検査・ビルド
cd frontend && npm run e2e                         # 画面のE2E（PC・スマートフォン、15件。API・Webの起動が必要）
API_DOWN=1 npm run e2e                             # APIを止めた状態のE2E（エラー表示・数値を作らないこと）
```

## 設計上の約束（変更しない）
- 架空の株価・企業データを実データのように表示しない。データがなければ「未接続」「データ不足」と表示し、値を出さない。
- 秘密情報（APIキー・DBパスワード）はサーバー側の `.env` だけに置き、ブラウザに送らない。
- 上場廃止になった銘柄も削除しない（生存者バイアス対策）。
- 本サービスは情報提供・学習を目的とした分析ツールであり、投資助言ではない。
