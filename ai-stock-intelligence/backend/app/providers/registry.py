"""プロバイダーの台帳。フェーズ3で各プロバイダーの取得処理を実装する。

フェーズ2の時点では、どのプロバイダーも株価・財務の取得処理を持たない。
したがって、設定の有無にかかわらず株価・財務は「未接続」になる（実装済みと偽らない）。
"""
from ..config import Settings
from .base import ProviderInfo, ProviderStatus

PROVIDERS: tuple[ProviderInfo, ...] = (
    ProviderInfo("jquants", "J-Quants API（日本取引所グループ）", ("prices_jp", "indices_jp", "financials_jp"),
                 ("JQUANTS_REFRESH_TOKEN",), "https://jpx-jquants.com/", "プランにより遅延・取得範囲が異なる"),
    ProviderInfo("us_prices", "米国株の株価API（フェーズ3で選定）", ("prices_us", "indices_us"),
                 ("US_PRICE_PROVIDER", "US_PRICE_API_KEY"), "", "候補を比較して選定する"),
    ProviderInfo("edinet", "EDINET API（金融庁）", ("financials_jp",), ("EDINET_API_KEY",),
                 "https://disclosure2.edinet-fsa.go.jp/", "有価証券報告書等"),
    ProviderInfo("sec_edgar", "SEC EDGAR（米国証券取引委員会）", ("financials_us", "filings_us"), ("SEC_USER_AGENT",),
                 "https://www.sec.gov/os/accessing-edgar-data", "連絡先を含むUser-Agentが必要"),
    ProviderInfo("fred", "FRED（セントルイス連銀）", ("macro", "fx", "indices_close"), (),
                 "https://fred.stlouisfed.org/", "経済指標・為替・一部指数の終値"),
)

# フェーズ2の時点で取得処理を実装済みのプロバイダー（フェーズ3で追加していく）
IMPLEMENTED: set[str] = set()

_ENV_TO_ATTR = {
    "JQUANTS_REFRESH_TOKEN": "jquants_refresh_token", "US_PRICE_PROVIDER": "us_price_provider",
    "US_PRICE_API_KEY": "us_price_api_key", "EDINET_API_KEY": "edinet_api_key", "SEC_USER_AGENT": "sec_user_agent",
}


def provider_statuses(settings: Settings) -> list[ProviderStatus]:
    out = []
    for p in PROVIDERS:
        missing = [env for env in p.requires if not getattr(settings, _ENV_TO_ATTR[env], "")]
        configured = not missing
        if p.key not in IMPLEMENTED:
            status, msg = "not_connected", "取得処理は未実装（フェーズ3で実装予定）"
        elif not configured:
            status, msg = "not_connected", "必要な設定が未入力"
        else:
            status, msg = "ok", ""
        out.append(ProviderStatus(key=p.key, name=p.name, datasets=p.datasets, configured=configured, status=status,
                                  message=msg, missing_settings=missing, url=p.url))
    return out
