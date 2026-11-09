"""API応答の型。すべての応答は、データの状態（data_status）と基準日時を持つ。"""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

DataStatus = Literal["ok", "stale", "not_connected", "insufficient", "error", "not_implemented"]


class ProviderOut(BaseModel):
    key: str
    name: str
    datasets: list[str]
    configured: bool
    status: DataStatus
    message: str
    missing_settings: list[str]
    url: str


class DatasetOut(BaseModel):
    dataset: str
    source: str
    status: str
    rows: int
    source_as_of: date | None
    finished_at: datetime | None


class DatasetStatus(BaseModel):
    dataset: str
    name: str
    provider: str
    reliability: str
    delay: str | None
    status: DataStatus
    configured: bool
    last_run_at: datetime | None
    last_run_status: str | None
    last_success_at: datetime | None
    latest_data_date: date | None
    rows: int
    message: str | None
    quality_issues: int
    warnings: list[str]
    # 株価のデータセットだけ：接続状態（7つの状態）・表示名・取得対象の銘柄数・株価のある銘柄数・最新の株価の取得元
    price_state: str | None = None
    price_state_label: str | None = None
    target_count: int | None = None
    fetched_count: int | None = None
    used_provider: str | None = None
    price_partial: bool | None = None       # 取得率：取得対象の一部だけ株価がある
    price_freshness: str | None = None      # 鮮度：fresh（提供元の遅延の範囲内）／stale（遅延を超えて古い）
    price_fallback: bool | None = None      # 最新の株価が代替の取得元のもの（米国株）
    target_label: str | None = None         # 取得対象（分母）の説明


class SystemStatus(BaseModel):
    app_version: str
    phase: str
    providers: list[ProviderOut]
    datasets: list[DatasetOut]
    data_sources: list[DatasetStatus]
    generated_at: datetime


class IndexQuote(BaseModel):
    key: str
    name: str
    market: str
    value: float | None
    change_pct: float | None
    as_of: date | datetime | None
    provider: str | None
    is_delayed: bool | None
    data_status: DataStatus
    note: str


class SecurityOut(BaseModel):
    market: str
    code: str
    cik: int | None = None
    name: str
    exchange: str | None
    sector: str | None
    size_class: str | None
    security_type: str
    is_active: bool
    source: str
    source_as_of: date | None


class Quote(BaseModel):
    price: float | None
    currency: str
    as_of: date | datetime | None
    fetched_at: datetime | None = None
    provider: str | None
    is_delayed: bool | None
    data_status: DataStatus
    note: str
    # 株価の接続状態（services/price_status.py の7つの状態）と、その表示名。as_of は株価の基準日、fetched_at は取得日時
    price_state: str | None = None
    price_state_label: str | None = None


class SecurityDetail(BaseModel):
    security: SecurityOut
    quote: Quote


class SearchItem(SecurityOut):
    """検索結果の1銘柄。株価の接続状態を添える（固定の「未接続」ではなく、実際の状態）。"""
    price_state: str | None = None
    price_state_label: str | None = None
    price_data_status: DataStatus | None = None


class SearchResult(BaseModel):
    query: str
    market: str | None
    total: int
    items: list[SearchItem]


class Section(BaseModel):
    """採点・予測・理論株価など、未実装または未接続の機能の状態を返すための共通の型。"""
    name: str
    data_status: DataStatus
    available_in_phase: int | None
    note: str
