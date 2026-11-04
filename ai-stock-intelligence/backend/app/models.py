"""データベースのテーブル定義（フェーズ2の範囲）。

フェーズ3以降で financial_facts、scores、forecasts（追記のみ）などを追加する（設計書 5章）。
"""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Security(Base):
    """銘柄マスタ。上場廃止になっても削除しない（生存者バイアス対策）。"""

    __tablename__ = "securities"
    __table_args__ = (UniqueConstraint("market", "code", name="uq_security_market_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(4), index=True)  # "JP" / "US"
    code: Mapped[str] = mapped_column(String(16), index=True)  # 証券コード / ティッカー
    cik: Mapped[int | None] = mapped_column(Integer, index=True)  # 米国：SECの企業番号（財務データの取得に使う）
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


class MacroSeries(Base):
    """経済指標・金利・為替・指数（終値）の系列の定義。"""

    __tablename__ = "macro_series"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # 例 "us_10y"
    source_id: Mapped[str] = mapped_column(String(64))  # 例 FREDの "DGS10"
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32))  # rates / inflation / labor / activity / fx / commodity / volatility / index / japan
    unit: Mapped[str] = mapped_column(String(32))
    frequency: Mapped[str] = mapped_column(String(16))  # daily / weekly / monthly / quarterly
    provider: Mapped[str] = mapped_column(String(32))
    reliability: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(512))


class MacroObservation(Base):
    __tablename__ = "macro_observations"
    __table_args__ = (UniqueConstraint("series_key", "obs_date", name="uq_macro_obs"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_key: Mapped[str] = mapped_column(ForeignKey("macro_series.key"), index=True)
    obs_date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[float] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IndexBar(Base):
    """株価指数の日次（FREDは終値のみ、J-Quants の TOPIX は四本値）。"""

    __tablename__ = "index_bars"
    __table_args__ = (UniqueConstraint("index_key", "trade_date", "provider", name="uq_index_bar"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_key: Mapped[str] = mapped_column(String(32), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FinancialFact(Base):
    """財務データ（時点つき）。同じ期間の値でも提出書類ごとに別の行として保存し、
    「その日に知り得た値」を後から再現できるようにする（設計書 5章、先読みバイアス対策）。"""

    __tablename__ = "financial_facts"
    __table_args__ = (UniqueConstraint("security_id", "tag", "unit", "period_start", "period_end", "accn",
                                       name="uq_fact"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), index=True)
    concept: Mapped[str] = mapped_column(String(48), index=True)  # 例 "revenue"（アプリ内の共通名）
    tag: Mapped[str] = mapped_column(String(128))  # 元のXBRLタグ（タグの変更・誤りの調査用）
    unit: Mapped[str] = mapped_column(String(24))
    period_start: Mapped[date | None] = mapped_column(Date)  # 時点の値（貸借対照表）は None
    period_end: Mapped[date] = mapped_column(Date, index=True)
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(8))  # FY / Q1 / Q2 / Q3
    form: Mapped[str] = mapped_column(String(16))  # 10-K / 10-Q / 20-F ...
    accn: Mapped[str] = mapped_column(String(32))
    filed: Mapped[date] = mapped_column(Date, index=True)  # SECへの提出日（この日以降にだけ使える）
    value: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DataQualityCheck(Base):
    """データ品質の検証結果（欠損・古さ・異常値・不整合）。"""

    __tablename__ = "data_quality_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset: Mapped[str] = mapped_column(String(64), index=True)
    subject: Mapped[str] = mapped_column(String(64))  # 系列キーや銘柄コード
    check_type: Mapped[str] = mapped_column(String(24))  # missing / stale / outlier / inconsistent
    severity: Mapped[str] = mapped_column(String(8))  # info / warn / error
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
