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


class SystemStatus(BaseModel):
    app_version: str
    phase: str
    providers: list[ProviderOut]
    datasets: list[DatasetOut]
    generated_at: datetime


class IndexQuote(BaseModel):
    key: str
    name: str
    market: str
    value: float | None
    change_pct: float | None
    as_of: datetime | None
    provider: str | None
    is_delayed: bool | None
    data_status: DataStatus
    note: str


class SecurityOut(BaseModel):
    market: str
    code: str
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
    as_of: datetime | None
    provider: str | None
    is_delayed: bool | None
    data_status: DataStatus
    note: str


class SecurityDetail(BaseModel):
    security: SecurityOut
    quote: Quote


class SearchResult(BaseModel):
    query: str
    market: str | None
    total: int
    items: list[SecurityOut]


class Section(BaseModel):
    """採点・予測・理論株価など、未実装または未接続の機能の状態を返すための共通の型。"""
    name: str
    data_status: DataStatus
    available_in_phase: int | None
    note: str
