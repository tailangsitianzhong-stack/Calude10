# 株式投資・金融市場 学習リポジトリ

10段階カリキュラムに沿って株式投資・金融市場を体系的に学習し、各段階で理解度テスト・実践課題・自己評価を行う記録。

## 構成

- `ai-stock-intelligence/docs/phase1_design.md` … Webアプリ「AI STOCK INTELLIGENCE」のフェーズ1設計書（承認待ち）
- `curriculum/00_curriculum.md` … 10段階カリキュラムの詳細分解（習得知識／必要データ／検証方法／合格基準）
- `stages/stage01_basics.md` … 第1段階：株式投資の基礎理論（本文・テスト・実践課題・自己評価）【合格】
- `stages/stage02_fundamentals.md` … 第2段階：企業分析・ファンダメンタルズ分析【合格（日本企業の演習は持ち越し）】
- `stages/stage03_technical.md` … 第3段階：テクニカル分析【合格（OHLCVを要する指標は持ち越し）】／全出力は `stages/stage03_backtest_results.md`
- `stages/stage04_macro.md` … 第4段階：マクロ経済・金融政策分析【合格（イベントスタディと日本の業種データは持ち越し）】／全出力は `stages/stage04_macro_results.md`
- `stages/stage05_strategies.md` … 第5段階：高度な投資戦略【合格（事例の一次資料確認とコスト込み再検証は持ち越し）】／全出力は `stages/stage05_strategy_results.md`
- `stages/stage06_growth.md` … 第6段階：将来有望な企業の発掘【合格（株価での検証と複数時点での再現は持ち越し）】／全出力は `stages/stage06_growth_screen_results.md`
- `stages/stage07_valuation.md` … 第7段階：企業価値と株価の予測【合格（PEPのFCF確認と持分法投資の調整は持ち越し）】／全出力は `stages/stage07_valuation_results.md`
- `stages/stage08_risk.md` … 第8段階：リスク管理【合格（日本の資産を含む検証と流動性リスクは持ち越し）】／全出力は `stages/stage08_risk_results.md`
- `stages/stage09_analysis.md` … 第9段階：実践的な市場分析（KO・PEP、13項目・投資判断・予測の記録）【合格】／補助計算 `stages/stage09_case_results.md`
- `stages/stage09b_sectors.md` … 第9段階（追加）：Micron（景気循環）・JPMorgan（銀行）の分析と、業種による評価方法の違い／全出力 `stages/stage09b_sector_results.md`
- `predictions/prediction_log.csv` … 確率付きの予測の記録（第10段階で採点）
- `stages/stage10_review.md` … 第10段階：学習内容の検証と改善（誤りの記録・再検証・統合した判断手順・到達度の評価）【合格（予測の採点は期限待ち）】／全出力 `stages/stage10_results.md`
- `tools/edgar_fundamentals.py` … SEC EDGAR XBRLから年次財務データを取得し指標を計算するスクリプト
- `tools/backtest.py` … FREDの日次データでテクニカル・ルールをバックテストするスクリプト
- `tools/macro_analysis.py` … French業種データとFREDで局面別リターン・金利感応度・逆イールド等を検証するスクリプト
- `tools/strategy_analysis.py` … ファクター・手法の代理ポートフォリオ・日本のファクター・バークシャーの成績を検証するスクリプト
- `tools/growth_screen.py` … SEC XBRL framesで過去時点の成長株スクリーニングを再現し、10年後を追跡するスクリプト
- `tools/valuation.py` … TTM財務・WACC・DCF（3シナリオ）・感応度・逆DCF・マルチプルの評価モデル（前提はファイル冒頭）
- `tools/risk_analysis.py` … 資産配分・ストレステスト・相関・為替・裾・ブートストラップ・ケリー・損切りを検証するスクリプト
- `tools/sector_cases.py` … 景気循環株の正常化利益・好況の継続年数の逆算、銀行の正当化P/TBVを計算するスクリプト
- `tools/investment_case.py` … 会社予想ベースのDCF・下落シナリオ・生活必需品株の過去の下落を計算するスクリプト
- `tools/robustness.py` … 基準率の別時点での再現、Newey-West によるt値の再計算
- `tools/score_predictions.py` … 予測の記録の採点（ブライアスコア、キャリブレーション、未判定の一覧）

## 運用原則
1. **事実・推測・予測を区別**する。本文では次のラベルを使う。
   - 【事実】一次情報・公的統計・学術文献で確認できる内容
   - 【通説】広く受け入れられているが条件付き・異論のある内容
   - 【推測】筆者（AI）の解釈・推論
   - 【要検証】最新値の確認が必要／記憶ベースで出典未照合
2. 数値には出典と時点を付ける。時点不明の数値は使わない。
3. 銘柄推奨・投資判断は第9段階に到達するまで行わない。
4. 投資判断では必ず反対意見・最悪シナリオ・損失額を併記する。
5. 本リポジトリは教育目的であり、投資助言ではない。

## AIの学習についての注意（正直な前提）
AIモデルはこの会話を通じて重みが更新されるわけではない。「学習」とは、知識を体系化し、
一次情報とデータで検証し、判断基準を文書化して次回以降に再利用できるようにすることを指す。
そのためこのリポジトリ自体を「検証済み知識ベース兼判断ログ」として扱う。
