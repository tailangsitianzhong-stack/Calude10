"""データベースのテーブル定義（フェーズ2の範囲）。

フェーズ3以降で financial_facts、scores、forecasts（追記のみ）などを追加する（設計書 5章）。
"""
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Security(Base):
    """銘柄マスタ。上場廃止になっても削除しない（生存者バイアス対策）。"""

    __tablename__ = "securities"
    __table_args__ = (UniqueConstraint("market", "code", name="uq_security_market_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(4), index=True)  # "JP" / "US"
    code: Mapped[str] = mapped_column(String(16), index=True)  # 証券コード / ティッカー
    name: Mapped[str] = mapped_column(String(256), index=True)  # 取得元の正式名（日本株は日本語）
    name_search: Mapped[str] = mapped_column(String(256), index=True)  # 検索用に正規化した名前
    exchange: Mapped[str | None] = mapped_column(String(64))  # 市場区分・取引所
    sector: Mapped[str | None] = mapped_column(String(128))  # 日本：33業種、米国：未取得
    size_class: Mapped[str | None] = mapped_column(String(64))  # 日本：TOPIXの規模区分
    security_type: Mapped[str] = mapped_column(String(32), default="common")  # common / etf / reit / other
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(64))  # 例 "JPX 上場銘柄一覧"
    source_as_of: Mapped[date | None] = mapped_column(Date)  # 取得元のデータの基準日
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DailyPrice(Base):
    """日次株価（フェーズ3で取得を実装）。プロバイダー・取得日時・遅延を必ず保存する。"""

    __tablename__ = "daily_prices"
    __table_args__ = (UniqueConstraint("security_id", "trade_date", "provider", name="uq_price"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delay_minutes: Mapped[int | None] = mapped_column(Integer)


class IngestionRun(Base):
    """データ取得の実行記録。画面の「データの状態」の根拠になる。"""

    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset: Mapped[str] = mapped_column(String(64), index=True)  # 例 "securities_jp"
    source: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))  # success / failed
    rows: Mapped[int] = mapped_column(Integer, default=0)
    source_as_of: Mapped[date | None] = mapped_column(Date)
    message: Mapped[str | None] = mapped_column(String(1024))
