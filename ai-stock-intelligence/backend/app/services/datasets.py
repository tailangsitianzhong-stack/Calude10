"""データセットごとの状態（取得日時・最新データ日・遅延・信頼性）を集計する。画面の「データの状態」の根拠。"""
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import DailyPrice, DataQualityCheck, FinancialFact, IndexBar, IngestionRun, JpDisclosure, MacroObservation, Security
from ..providers import jquants

DATASETS = {
    "fred": {"name": "経済指標・金利・為替・株価指数（終値）", "provider": "FRED（セントルイス連銀）",
             "reliability": "公的統計・公式データ（FRED経由）", "delay": "日次系列は前営業日まで（翌営業日に更新）。経済指標は公表日に更新、後日改定あり"},
    "sec_financials": {"name": "米国企業の財務データ", "provider": "SEC EDGAR（XBRL）",
                       "reliability": "一次情報（企業がSECに提出した書類）", "delay": "提出から数分〜1営業日でAPIに反映"},
    "financials_jp": {"name": "日本企業の財務データ", "provider": "J-Quants API V2（財務情報 /v2/fins/summary）",
                      "reliability": "公式データ（日本取引所グループ。上場会社が提出したXBRLに基づく）", "delay": None},
    "prices_jp": {"name": "日本株の株価・TOPIX", "provider": "J-Quants API V2",
                  "reliability": "公式データ（日本取引所グループ）", "delay": None},
    "prices_us": {"name": "米国株の株価", "provider": "Tiingo / Massive（選択制）",
                  "reliability": "データ提供会社（取引所データの再配布）", "delay": "日足（終値ベース）"},
    "securities_jp": {"name": "日本株の銘柄一覧", "provider": "JPX 東証上場銘柄一覧",
                      "reliability": "公式データ（日本取引所グループ）", "delay": "月次更新"},
    "securities_us": {"name": "米国株の銘柄一覧", "provider": "Nasdaq Trader Symbol Directory",
                      "reliability": "公式データ（Nasdaq）", "delay": "日次更新"},
}


def dataset_status(session: Session) -> list[dict]:
    st = get_settings()
    out = []
    for key, meta in DATASETS.items():
        run = session.scalar(select(IngestionRun).where(IngestionRun.dataset == key).order_by(IngestionRun.id.desc()).limit(1))
        ok_run = session.scalar(select(IngestionRun).where(IngestionRun.dataset == key, IngestionRun.status.in_(("success", "partial")))
                                .order_by(IngestionRun.id.desc()).limit(1))
        latest_data, rows = None, 0
        if key == "fred":
            latest_data = session.scalar(select(func.max(MacroObservation.obs_date)))
            rows = session.scalar(select(func.count()).select_from(MacroObservation)) or 0
        elif key == "sec_financials":
            # 米国企業だけを数える（日本企業の財務データも financial_facts に入るため）
            us_ids = select(Security.id).where(Security.market == "US")
            latest_data = session.scalar(select(func.max(FinancialFact.filed)).where(FinancialFact.security_id.in_(us_ids)))
            rows = session.scalar(select(func.count(func.distinct(FinancialFact.security_id)))
                                  .where(FinancialFact.security_id.in_(us_ids))) or 0
        elif key == "financials_jp":
            latest_data = session.scalar(select(func.max(JpDisclosure.disc_date)))
            rows = session.scalar(select(func.count(func.distinct(JpDisclosure.security_id)))
                                  .where(JpDisclosure.security_id.is_not(None))) or 0
        elif key in ("prices_jp", "prices_us"):
            prov = ("jquants",) if key == "prices_jp" else ("tiingo", "massive")
            latest_data = session.scalar(select(func.max(DailyPrice.trade_date)).where(DailyPrice.provider.in_(prov)))
            rows = session.scalar(select(func.count(func.distinct(DailyPrice.security_id))).where(DailyPrice.provider.in_(prov))) or 0
        elif ok_run:
            latest_data, rows = ok_run.source_as_of, ok_run.rows
        delay = meta["delay"]
        if key in ("prices_jp", "financials_jp"):
            delay = jquants.DELAY_NOTE.get(st.jquants_plan, "") if st.has_credential("jquants") else "未接続"
        configured = {"prices_jp": st.has_credential("jquants"), "financials_jp": st.has_credential("jquants"),
                      "prices_us": bool(st.us_price_provider and st.has_credential(st.us_price_provider)),
                      "sec_financials": st.sec_ua_configured}.get(key, True)
        # SEC は設定がなくなっても、過去に取得した財務データは表示する（更新が止まっている旨を警告する）
        if not configured and (key != "sec_financials" or not rows):
            status = "not_connected"
        elif run and run.status == "failed" and not rows:
            status = "error"  # 設定はあるが、取得元の障害などで1件も取得できていない（「未接続」とは区別する）
        elif ok_run is None and not rows:
            status = "not_connected"
        else:
            status = "ok"
            # 取得自体は成功していても、最新データが古ければ「古いデータ」と表示する（例：Freeプランの12週間遅延）
            if key in ("prices_jp", "prices_us") and latest_data and (date.today() - latest_data).days > 7:
                status = "stale"
            if key in ("prices_jp", "prices_us") and run and run.status == "failed":
                status = "error"  # 保存済みの値はあるが、最新の取得は取得元の障害で失敗している
        qkey = {"fred": "fred", "sec_financials": "sec", "financials_jp": "jquants_fins"}.get(key, key)
        issues = session.scalar(select(func.count()).select_from(DataQualityCheck).where(DataQualityCheck.dataset == qkey)) or 0
        warnings = []
        if key == "sec_financials" and not st.sec_ua_configured:
            warnings.append("SEC_USER_AGENT（アプリ名と連絡先メールアドレス）が未設定のため、SECの財務データを更新していません")
        if key == "prices_jp" and st.jquants_refresh_token and not st.has_credential("jquants"):
            warnings.append("JQUANTS_REFRESH_TOKEN（V1）は V2 では使えません。ダッシュボードで発行した APIキーを JQUANTS_API_KEY に設定してください")
        if key in ("prices_jp", "prices_us") and run and run.status == "failed":
            warnings.append("株価の最新の取得に失敗しました（取得元の障害）。" +
                            ("日本株の株価に代わりの取得元はありません" if key == "prices_jp" else "代わりの取得元でも取得できませんでした"))
        if key == "fred" and run and run.message and "FRED_API_KEY" in run.message:
            warnings.append("FRED_API_KEY が受け付けられなかったため、公開CSVで取得しています")
        if key == "financials_jp" and st.has_credential("jquants") and st.jquants_plan == "free":
            warnings.append("Freeプランのため、決算短信の財務情報は開示から約12週間後のものまでしか取得できません")
        if key == "financials_jp" and rows:
            warnings.append("負債の合計は決算短信のサマリーに項目がないため未取得（EDINET での補完を検討中）")
        if key == "prices_jp" and st.has_credential("jquants") and st.jquants_plan == "free":
            warnings.append("Freeプランのため株価は約12週間遅れています。現在の株価ではありません")
        out.append({"dataset": key, **meta, "delay": delay, "status": status, "configured": configured,
                    "last_run_at": run.finished_at if run else None, "last_run_status": run.status if run else None,
                    "last_success_at": ok_run.finished_at if ok_run else None, "latest_data_date": latest_data,
                    "rows": rows, "message": run.message if run else None, "quality_issues": issues, "warnings": warnings})
    return out
