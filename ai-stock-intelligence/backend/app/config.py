"""設定。秘密情報は環境変数（.env）からだけ読み込み、API応答には含めない。

環境変数名（大文字）とフィールド名（小文字）が対応する。例：JQUANTS_API_KEY → jquants_api_key
.env は次の順に探す（あとのファイルの値が優先）：
  1. プロジェクト直下 ai-stock-intelligence/.env（Docker Compose と同じファイル）
  2. backend/.env（任意）
  3. 実行時のカレントディレクトリの .env（任意）
実際の環境変数（Docker の env_file など）は .env より優先される。
"""
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# SECの規則：User-Agentにアプリ名と連絡先を含める。専用アドレスが決まるまでの仮設定（画面に「仮設定」と表示）
SEC_PROVISIONAL_UA = "AI-STOCK-INTELLIGENCE/0.3 provisional-contact@example.com"

_BACKEND = Path(__file__).resolve().parents[1]
ENV_FILES = (_BACKEND.parent / ".env", _BACKEND / ".env", Path(".env"))

US_PRICE_PROVIDERS = ("", "tiingo", "massive", "polygon")
JQUANTS_PLANS = ("free", "light", "standard", "premium")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILES, env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./data/asi.sqlite3"

    # J-Quants API V2：ダッシュボードで発行したAPIキー（x-api-key ヘッダーで送る）
    jquants_api_key: str = ""
    jquants_plan: str = "free"  # free / light / standard / premium（遅延・アクセス間隔に使う）
    # V1のリフレッシュトークン（V2では使えない）。設定されていたら画面で注意を出すためだけに読む
    jquants_refresh_token: str = ""

    # 米国株の株価：tiingo / massive（旧 Polygon.io。polygon と書いても同じ）
    us_price_provider: str = ""
    tiingo_api_key: str = ""
    # Massive（旧 Polygon.io）のキー：MASSIVE_API_KEY を優先し、空なら旧名 POLYGON_API_KEY を使う
    # （空の MASSIVE_API_KEY= が .env にあっても POLYGON_API_KEY が無視されないよう、別々に読んでから選ぶ）
    massive_api_key_raw: str = Field("", validation_alias="MASSIVE_API_KEY")
    polygon_api_key: str = ""
    massive_base_url_raw: str = Field("", validation_alias="MASSIVE_BASE_URL")
    polygon_base_url: str = ""

    # SEC EDGAR（キー不要。User-Agent にアプリ名と連絡先が必要）
    sec_user_agent: str = ""
    sec_universe_size: int = 150  # 財務データを取得する米国株の数（公開流通時価総額の大きい順）
    sec_extra_tickers: str = "KO,PEP,MU,JPM,KDP"

    # FRED：未設定なら公開CSV（fredgraph.csv）、設定すれば公式API（api.stlouisfed.org）を使う
    fred_api_key: str = ""

    # 将来のフェーズで使う予定（現在は読み込むだけで使っていない）
    edinet_api_key: str = ""
    anthropic_api_key: str = ""

    @field_validator("sec_universe_size", mode="before")
    @classmethod
    def _size_default(cls, v):
        return 150 if str(v).strip() == "" else v

    @field_validator("sec_extra_tickers", mode="before")
    @classmethod
    def _tickers_default(cls, v):
        return (v or "").strip() or "KO,PEP,MU,JPM,KDP"

    @field_validator("database_url", mode="before")
    @classmethod
    def _db_default(cls, v):
        # .env の「DATABASE_URL=」（空）は未指定として扱い、SQLite を使う
        return (v or "").strip() or "sqlite:///./data/asi.sqlite3"

    @field_validator("jquants_plan", "us_price_provider", mode="before")
    @classmethod
    def _lower(cls, v):
        return (v or "").strip().lower()

    @field_validator("jquants_plan", mode="before")
    @classmethod
    def _plan_default(cls, v):
        return (v or "").strip().lower() or "free"

    @field_validator("jquants_plan")
    @classmethod
    def _plan(cls, v):
        if v not in JQUANTS_PLANS:
            raise ValueError(f"JQUANTS_PLAN は {', '.join(JQUANTS_PLANS)} のいずれか（指定値：{v}）")
        return v

    @field_validator("us_price_provider")
    @classmethod
    def _provider(cls, v):
        if v not in US_PRICE_PROVIDERS:
            raise ValueError(f"US_PRICE_PROVIDER は tiingo / massive / polygon のいずれか（指定値：{v}）")
        return "massive" if v == "polygon" else v

    @property
    def massive_api_key(self) -> str:
        return self.massive_api_key_raw.strip() or self.polygon_api_key.strip()

    @property
    def massive_base_url(self) -> str:
        return self.massive_base_url_raw.strip() or self.polygon_base_url.strip() or "https://api.polygon.io"

    @property
    def sec_ua(self) -> str:
        return self.sec_user_agent.strip() or SEC_PROVISIONAL_UA

    @property
    def sec_ua_is_provisional(self) -> bool:
        ua = self.sec_ua.lower()
        return "example.com" in ua or "provisional" in ua or "@" not in ua


@lru_cache
def get_settings() -> Settings:
    return Settings()
