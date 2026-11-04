"""設定。秘密情報は環境変数（.env）からだけ読み込み、API応答には含めない。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# SECの規則：User-Agentにアプリ名と連絡先を含める。専用アドレスが決まるまでの仮設定（画面に「仮設定」と表示）
SEC_PROVISIONAL_UA = "AI-STOCK-INTELLIGENCE/0.3 provisional-contact@example.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/asi.sqlite3"
    # J-Quants API V2：ダッシュボードで発行したAPIキー（x-api-key ヘッダーで送る）
    jquants_api_key: str = ""
    jquants_plan: str = "free"  # free / light / standard / premium（遅延・レート制限の表示に使う）
    # 米国株の株価：tiingo / massive（旧 Polygon.io）のどちらか
    us_price_provider: str = ""
    tiingo_api_key: str = ""
    massive_api_key: str = ""
    massive_base_url: str = "https://api.polygon.io"
    edinet_api_key: str = ""
    sec_user_agent: str = ""
    sec_universe_size: int = 150  # SECの財務データを取得する米国株の数（公開流通時価総額の大きい順）
    sec_extra_tickers: str = "KO,PEP,MU,JPM,KDP"
    fred_api_key: str = ""
    anthropic_api_key: str = ""

    @property
    def sec_ua(self) -> str:
        return self.sec_user_agent.strip() or SEC_PROVISIONAL_UA

    @property
    def sec_ua_is_provisional(self) -> bool:
        ua = self.sec_ua.lower()
        return "example.com" in ua or "provisional" in ua


@lru_cache
def get_settings() -> Settings:
    return Settings()
