# フェーズ3：データソース — 実装状況・米国株API比較・接続手順

> 作成日：2026-11-04。料金・制限は各社の公式ページ（取得できた範囲）と第三者の情報をもとにした**2026年11月時点の確認結果**です。
> 【公式】＝公式ページで確認、【二次】＝第三者の情報のみ（契約前に公式ページで要確認）。

---

## 1. 実装状況（2026-11-04 時点）

| データ | 取得元 | 状態 | 動作確認 |
|---|---|---|---|
| 米国企業の財務データ（売上・利益・CF・BS・EPS・配当・自社株買い等 27項目） | SEC EDGAR XBRL | **取得済み**（181社、約62万件、提出日つき） | 実データで確認。第7段階で手計算した KO・PEP・JPM の値と一致 |
| 金利・インフレ・雇用・景気・為替・原油・VIX（22系列） | FRED | **取得済み**（日次・月次・四半期） | 実データで確認 |
| 株価指数の終値：日経平均・S&P 500・NASDAQ総合・ダウ平均 | FRED | **取得済み** | 実データで確認 |
| 銘柄一覧（日本4,447・米国13,239） | JPX・Nasdaq Trader | 取得済み（フェーズ2） | 実データで確認 |
| 日本株の株価・TOPIX | J-Quants API V2 | **実装済み・未接続**（APIキー待ち） | 公式仕様の書式でテストのみ。実接続は未確認 |
| 米国株の株価 | Tiingo / Massive（選択制） | **実装済み・未接続**（APIキー待ち） | 同上 |
| 日本企業の財務 | J-Quants（/v2/fins/summary）・EDINET | **未実装** | — |
| ニュース・開示 | SEC 8-K・TDnet 等 | **未実装**（フェーズ4で一次情報から） | — |

**品質検証が実データで見つけたもの**（いずれも画面に「古いデータ」「品質の注意」として表示）
- WTI原油（FRED）の更新が8日間止まっている。
- Citigroup の2026年の四半期報告（10-Q）が SEC の財務APIに入っていない（SEC側のデータの欠落）。
- 米CPIの2025年10月分が欠損（FRED上で値なし）。

---

## 2. 日本株：J-Quants API V2（採用）

**仕様（公式ドキュメント 2026-11 確認）【公式】**
| 項目 | 内容 |
|---|---|
| ベースURL | `https://api.jquants.com` |
| 認証 | ダッシュボードで発行した **APIキー** を `x-api-key` ヘッダーで送る（V1のトークン方式は廃止。`Authorization` と同時に送ると 403） |
| 応答 | `{"data": [...], "pagination_key": "..."}`。`pagination_key` がある間は続きを取得 |
| 株価四本値 | `/v2/equities/bars/daily`（`code` か `date` が必須）。列名は短縮形：`O H L C Vo Va AdjC AdjFactor MktCap` など |
| TOPIX | `/v2/indices/bars/daily/topix` |
| 上場銘柄一覧 | `/v2/equities/master` |
| 財務情報 | `/v2/fins/summary`（本アプリでは未実装） |
| レート制限（リクエスト/分） | Free 5、Light 60、Standard 120、Premium 500 |
| Freeプラン | 料金0円、**データは約12週間遅延**、過去2年分 |

**本アプリでの扱い**
- Free プランでは、画面の株価に「約12週間遅延（Freeプラン）。リアルタイムの株価ではありません」と常に表示し、上部にも警告を出す。
- 5リクエスト/分に合わせ、**日付ごとの一括取得**（1日分の全銘柄を1リクエスト＋ページ送り）で、遅延の分だけ遡った日から過去に向けて取得する。1回の実行は40営業日まで（約8分）。2年分（約490営業日）は約12回の実行で揃う（定期実行で自動的に進む）。
- 12週間遅延のデータは、**現在の株価として使えない**。採点（フェーズ4）では、日本株の株価に依存する項目（テクニカル・割安度）に「12週間前の株価に基づく」と明示し、信頼度を下げる。

---

## 3. 米国株：Massive（旧 Polygon.io）と Tiingo の比較

| 条件 | Massive（旧 Polygon.io） | Tiingo |
|---|---|---|
| **無料プラン** | Stocks Basic：$0【二次】 | Starter：$0【公式】 |
| **過去の株価の期間（無料）** | **2年**【二次】 | **30年以上**【公式】 |
| 過去の株価の期間（有料） | Starter 5年、Developer 10年、Advanced 20年以上【二次】 | 30年以上（全プラン同じ）【公式】 |
| **現在株価の更新頻度** | 無料：日足（前営業日の終値）。Starter・Developer：15分遅延、Advanced（$199）：リアルタイム【二次】 | 日足は米国東部時間17:30頃に更新【公式】。IEX の日中データあり（プラン別の範囲は未確認） |
| **財務データ** | 「Financials & Ratios」は $29/月の別商品、または Advanced に含む【公式】 | 株式30,025銘柄。無料は5年分、Power は15年以上【公式】（※別途、第三者提供の追加プランの記載もあり要確認） |
| **出来高** | あり（日足・分足） | あり（日足） |
| **ニュース** | Benzinga のニュースは $99/月の追加【公式】。無料プランでのニュースの可否は未確認 | News API：3か月分の履歴＋以後のデータ【公式】。無料プランでの利用範囲は要確認 |
| **APIの利用制限（無料）** | **5リクエスト/分**【二次】。超えると 429 | **50リクエスト/時、1,000/日、月500銘柄、月1GB**【公式】 |
| 利用制限（有料） | Starter 以上は無制限【二次】 | Power：10,000/時、100,000/日、月40GB【公式】 |
| **月額料金** | Starter $29、Developer $79、Advanced $199（個人向け）【二次・一部公式】 | Power $30、商用（社内利用）$50/月 または $499/年【公式】 |
| **個人利用と商用利用** | 公開されている株式プランは「Individual use only」。法人向けは別料金（非公開）【公式】 | 無料・Power は「Internal use only（自分だけの利用。他人に表示・共有できない）」。商用の社内利用は $50/月。再配布は別契約【公式】 |
| **銘柄数の拡張性** | 全銘柄の1日分を1リクエストで取得できる（grouped daily）。無料でも1日1回の更新なら全米国株を扱える | 無料は月500銘柄まで。全銘柄を扱うには Power（$30）が必要。1銘柄1リクエストのため、更新に時間がかかる |
| 本アプリの実装 | `US_PRICE_PROVIDER=massive`（grouped daily で日付ごとに取得） | `US_PRICE_PROVIDER=tiingo`（銘柄ごとに全期間を取得） |

### 推奨：**Tiingo（まず無料の Starter、銘柄を増やす段階で Power $30）**

**理由**
1. **予測の検証（フェーズ6）には長い株価の履歴が不可欠**。第3段階の学習で、短い期間の検証は誤った結論を出すこと（前半で良かった設定が後半で最悪になった）を確認した。Massive の無料は2年分で、景気後退やインフレ局面を含む検証ができない。Tiingo は無料でも30年以上ある。
2. **財務データは SEC EDGAR から無料の一次情報を取得済み**。API側の財務データに頼る必要が小さく、Massive の財務（$29の別商品）の利点が小さい。
3. 1人用の個人利用という承認済みの範囲では、Tiingo の「Internal use only」の条件に合う。
4. 無料の制限（50リクエスト/時、月500銘柄）は、S&P500 規模（約500銘柄）でちょうど収まる。初回は1銘柄1リクエストで全期間を取れるため、500銘柄で約10時間（自動の定期実行で完了）。

**反対意見・注意点**
- 毎日の更新も1銘柄1リクエストのため、500銘柄の更新に約10時間かかる（翌朝までには終わる）。毎日すべての銘柄を早く更新したい場合は、Massive の grouped daily（1日1リクエスト）の方が効率的。**両方を併用できる設計**にしてあるので、後から追加・切り替えできる。
- 月500銘柄の上限は、日本株を除いても余裕が小さい。対象を増やすなら Power（$30/月）が必要。
- 将来、他人に見せる・公開する場合は、どちらの無料プランも条件を満たさない（商用・再配布の契約が必要）。

---

## 4. 接続手順

### 4.1 共通
1. プロジェクトの `ai-stock-intelligence/.env.example` を `.env` にコピーする（`.env` は Git に含まれない）。
2. 値を設定したら、`docker compose up -d` で再起動する。worker が定期的に取得する（FRED 6時間ごと、SEC・株価 12〜24時間ごと）。
3. すぐに取得したい場合：`docker compose run --rm worker python -m app.worker <fred|sec|jquants|us_prices>`
4. 画面の「データ」ページ（`/data`）で、状態・最新データの日付・最終取得日時・警告を確認する。

### 4.2 J-Quants（日本株）
1. https://jpx-jquants.com/ でユーザー登録し、Free プランを選ぶ。
2. ダッシュボードで **APIキー** を発行する（V2 は APIキー方式。リフレッシュトークンではない）。
3. `.env` に設定：
   ```
   JQUANTS_API_KEY=発行したAPIキー
   JQUANTS_PLAN=free
   ```
4. `docker compose run --rm worker python -m app.worker jquants` を実行。「データ」ページで「日本株の株価・TOPIX」が「取得済み」になり、最新データの日付が約12週間前になっていることを確認する。
5. 認証に失敗した場合は、ログに「J-Quants の認証に失敗しました（401/403）」と表示される。

### 4.3 Tiingo（米国株・推奨）
1. https://www.tiingo.com/ で登録し、アカウントの API ページでトークンを確認する。
2. `.env` に設定：
   ```
   US_PRICE_PROVIDER=tiingo
   TIINGO_API_KEY=トークン
   ```
3. `docker compose run --rm worker python -m app.worker us_prices`。1回の実行で40銘柄（無料プランの上限に合わせて72秒に1件）を取得し、定期実行で残りを取得する。

### 4.4 Massive（米国株・代替または併用）
```
US_PRICE_PROVIDER=massive
MASSIVE_API_KEY=キー
MASSIVE_BASE_URL=https://api.polygon.io
```

### 4.5 SEC EDGAR（財務データ）
SECの規則により、アプリ名と連絡先メールアドレスを User-Agent に含めます。専用アドレスが決まったら：
```
SEC_USER_AGENT=AI-STOCK-INTELLIGENCE/0.3 専用アドレス@ドメイン
```
未設定の間は仮の値（`provisional-contact@example.com`）で動き、画面に「仮設定」の警告を表示します。仮の値のまま長く使うと、SEC からアクセスを制限される可能性があります。

---

## 5. 採点（フェーズ4）と予測（フェーズ5）に使えるデータ

| 必要なデータ | 現在 | 採点・予測での用途 |
|---|---|---|
| 米国企業の財務（時点つき） | **あり**（181社） | 成長性・財務健全性・競争優位性・経営（資本配分）。過去の時点で知り得た値だけを使える |
| 経済指標・金利・為替 | **あり** | 経済・市場環境の項目、市場環境モデル |
| 米国株の株価 | **なし**（Tiingo のキー待ち） | 割安度（PER・逆DCF）、テクニカル、予測モデルのすべて |
| 日本株の株価 | **なし**（J-Quants のキー待ち。Free は12週間遅延） | 同上（日本株） |
| 日本企業の財務 | **なし** | 日本株の成長性・健全性 |

株価がない銘柄は、株価に依存する項目を「採点不能」とし、設計どおりカバー率70%未満なら総合点を表示しません。**株価APIの接続が、採点と予測を実データで動かすための最大の前提**です。

---

## 参照した情報源
- J-Quants「V1 API から V2 API への変更点」https://jpx-jquants.com/ja/spec/migration-v1-v2 、株価四本値 https://jpx-jquants.com/ja/spec/eq-bars-daily 、TOPIX https://jpx-jquants.com/ja/spec/idx-bars-daily-topix
- Tiingo 料金 https://www.tiingo.com/about/pricing 、EOD https://www.tiingo.com/products/end-of-day-stock-price-data
- Massive 料金 https://massive.com/pricing
- 第三者の比較（Massive の無料プランの詳細）：https://algodataindex.com/vendors/massive/ 、https://qveris.ai/guides/polygon-pricing-optimized 、https://apicostcalc.com/polygon.html
- SEC「Accessing EDGAR Data」https://www.sec.gov/os/accessing-edgar-data
