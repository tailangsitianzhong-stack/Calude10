"""設定。秘密情報は環境変数（.env）からだけ読み込み、API応答には含めない。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/asi.sqlite3"
    jquants_refresh_token: str = ""
    us_price_provider: str = ""
    us_price_api_key: str = ""
    edinet_api_key: str = ""
    sec_user_agent: str = ""
    fred_api_key: str = ""
    anthropic_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
