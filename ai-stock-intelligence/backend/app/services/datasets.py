"""データセットごとの状態（取得日時・最新データ日・遅延・信頼性）を集計する。画面の「データの状態」の根拠。"""
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import DailyPrice, DataQualityCheck, FinancialFact, IndexBar, IngestionRun, MacroObservation
from ..providers import jquants

DATASETS = {
    "fred": {"name": "経済指標・金利・為替・株価指数（終値）", "provider": "FRED（セントルイス連銀）",
             "reliability": "公的統計・公式データ（FRED経由）", "delay": "日次系列は前営業日まで（翌営業日に更新）。経済指標は公表日に更新、後日改定あり"},
    "sec_financials": {"name": "米国企業の財務データ", "provider": "SEC EDGAR（XBRL）",
                       "reliability": "一次情報（企業がSECに提出した書類）", "delay": "提出から数分〜1営業日でAPIに反映"},
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
            latest_data = session.scalar(select(func.max(FinancialFact.filed)))
            rows = session.scalar(select(func.count(func.distinct(FinancialFact.security_id)))) or 0
        elif key in ("prices_jp", "prices_us"):
            prov = ("jquants",) if key == "prices_jp" else ("tiingo", "massive")
            latest_data = session.scalar(select(func.max(DailyPrice.trade_date)).where(DailyPrice.provider.in_(prov)))
            rows = session.scalar(select(func.count(func.distinct(DailyPrice.security_id))).where(DailyPrice.provider.in_(prov))) or 0
        elif ok_run:
            latest_data, rows = ok_run.source_as_of, ok_run.rows
        delay = meta["delay"]
        if key == "prices_jp":
            delay = jquants.DELAY_NOTE.get(st.jquants_plan, "") if st.jquants_api_key else "未接続"
        configured = {"prices_jp": bool(st.jquants_api_key),
                      "prices_us": bool(st.us_price_provider and (st.tiingo_api_key or st.massive_api_key))}.get(key, True)
        if not configured:
            status = "not_connected"
        elif ok_run is None:
            status = "not_connected"
        elif run and run.status == "failed":
            status = "error"
        else:
            status = "ok"
        issues = session.scalar(select(func.count()).select_from(DataQualityCheck).where(
            DataQualityCheck.dataset == key.split("_")[0] if key in ("fred", "sec_financials") else DataQualityCheck.dataset == key)) or 0
        warnings = []
        if key == "sec_financials" and st.sec_ua_is_provisional:
            warnings.append("SECへの連絡先（User-Agent）が仮設定です。専用のメールアドレスに変更してください")
        if key == "prices_jp" and st.jquants_api_key and st.jquants_plan == "free":
            warnings.append("Freeプランのため株価は約12週間遅れています。現在の株価ではありません")
        out.append({"dataset": key, **meta, "delay": delay, "status": status, "configured": configured,
                    "last_run_at": run.finished_at if run else None, "last_run_status": run.status if run else None,
                    "last_success_at": ok_run.finished_at if ok_run else None, "latest_data_date": latest_data,
                    "rows": rows, "message": run.message if run else None, "quality_issues": issues, "warnings": warnings})
    return out
