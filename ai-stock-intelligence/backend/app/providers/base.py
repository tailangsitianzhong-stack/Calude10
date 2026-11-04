"""データプロバイダーの共通インターフェース（設計書 4章）。

プロバイダーはAPIキーが設定されていなければ「未接続（not_connected）」を返す。
未接続のときに代わりの架空データを返すことは、どのプロバイダーにも許さない。
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

DataStatus = Literal["ok", "stale", "not_connected", "insufficient", "error"]


@dataclass(frozen=True)
class ProviderInfo:
    key: str  # 例 "jquants"
    name: str  # 画面表示名
    datasets: tuple[str, ...]  # 例 ("prices_jp", "financials_jp", "indices_jp")
    requires: tuple[str, ...]  # 必要な設定（環境変数名）
    url: str  # 提供元のURL（利用規約の確認先）
    note: str = ""


@dataclass
class ProviderStatus:
    key: str
    name: str
    datasets: tuple[str, ...]
    configured: bool
    status: DataStatus
    last_success_at: datetime | None = None
    delay_note: str | None = None
    message: str = ""
    missing_settings: list[str] = field(default_factory=list)
    url: str = ""
