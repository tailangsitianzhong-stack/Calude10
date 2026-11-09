"""プロバイダーの台帳。フェーズ3で各プロバイダーの取得処理を実装する。

フェーズ2の時点では、どのプロバイダーも株価・財務の取得処理を持たない。
したがって、設定の有無にかかわらず株価・財務は「未接続」になる（実装済みと偽らない）。
"""
from ..config import Settings
from .base import ProviderInfo, ProviderStatus

PROVIDERS: tuple[ProviderInfo, ...] = (
    ProviderInfo("jquants", "J-Quants API V2（日本取引所グループ）", ("prices_jp", "indices_jp", "financials_jp"),
                 ("JQUANTS_API_KEY",), "https://jpx-jquants.com/", "Freeプランは約12週間遅延"),
    ProviderInfo("us_prices", "米国株の株価API（Tiingo / Massive）", ("prices_us",),
                 ("US_PRICE_PROVIDER", "TIINGO_API_KEY or MASSIVE_API_KEY"), "", "docs/phase3_data_sources.md 参照"),
    ProviderInfo("edinet", "EDINET API（金融庁）", ("financials_jp",), ("EDINET_API_KEY",),
                 "https://disclosure2.edinet-fsa.go.jp/", "有価証券報告書・半期報告書（日本企業の負債の合計の補完）"),
    ProviderInfo("sec_edgar", "SEC EDGAR（米国証券取引委員会）", ("financials_us", "filings_us"), ("SEC_USER_AGENT",),
                 "https://www.sec.gov/os/accessing-edgar-data", "連絡先を含むUser-Agentが必要"),
    ProviderInfo("fred", "FRED（セントルイス連銀）", ("macro", "fx", "indices_close"), (),
                 "https://fred.stlouisfed.org/", "経済指標・為替・一部指数の終値"),
)

# 取得処理を実装済みのプロバイダー（EDINET は日本企業の負債の補完として実装済み：services/ingest_edinet.py）
IMPLEMENTED: set[str] = {"jquants", "us_prices", "sec_edgar", "fred", "edinet"}

def _is_set(settings: Settings, env: str) -> bool:
    if env == "TIINGO_API_KEY or MASSIVE_API_KEY":
        return settings.has_credential("tiingo") or settings.has_credential("massive")
    if env == "SEC_USER_AGENT":
        return settings.sec_ua_configured  # 仮の値では接続しない
    if env == "JQUANTS_API_KEY":
        return settings.has_credential("jquants")
    return bool(getattr(settings, env.lower(), ""))


def provider_statuses(settings: Settings) -> list[ProviderStatus]:
    out = []
    for p in PROVIDERS:
        missing = [env for env in p.requires if not _is_set(settings, env)]
        configured = not missing
        if p.key not in IMPLEMENTED:
            status, msg = "not_connected", "取得処理は未実装"
        elif not configured and p.key == "sec_edgar":
            status, msg = "not_connected", "SEC_USER_AGENT 未設定（アプリ名と連絡先メールアドレスが必要）"
        elif not configured:
            status, msg = "not_connected", "APIキー未設定（.env で設定）"
        elif p.key == "edinet":
            status, msg = "ok", "接続設定済み（有価証券報告書・半期報告書から負債の合計を補完）"
        elif p.key == "fred":
            status, msg = "ok", "公式API（FRED_API_KEY）" if settings.fred_api_key else "公開CSV（キー不要）"
        else:
            status, msg = "ok", "接続設定済み"
        out.append(ProviderStatus(key=p.key, name=p.name, datasets=p.datasets, configured=configured, status=status,
                                  message=msg, missing_settings=missing, url=p.url))
    return out
