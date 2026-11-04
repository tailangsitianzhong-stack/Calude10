"""FRED（セントルイス連銀）からの経済指標・金利・為替・指数（終値）の取得。

FRED_API_KEY が未設定なら公開の fredgraph.csv を使う（キー不要）。
設定されていれば公式API（https://api.stlouisfed.org/fred/series/observations）を使い、キーが無効なときは CSV に切り替えて続行する。
注意：
- FREDの日次系列は、多くが翌営業日に更新される（＝前営業日までの値）。リアルタイムではない。
- 経済指標（CPI・雇用統計など）は公表後に改定されることがあり、ここで保存するのは「取得時点の最新値」。
  過去時点で知り得た値（vintage）が必要な検証では ALFRED を使う必要がある（フェーズ6で対応）。
- S&P 500 と NASDAQ・日経平均の値は、各指数の提供元がFREDに提供しているもの。FREDでは S&P 500 は直近10年分のみ公開。
"""
import csv
import io
from dataclasses import dataclass
from datetime import date, datetime

from .http import RateLimitedClient, check

BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
API = "https://api.stlouisfed.org/fred/series/observations"


@dataclass(frozen=True)
class SeriesDef:
    key: str
    fred_id: str
    name: str
    category: str
    unit: str
    frequency: str
    note: str = ""


SERIES: tuple[SeriesDef, ...] = (
    # 金利
    SeriesDef("us_ff_rate", "DFF", "米FF金利（実効）", "rates", "%", "daily"),
    SeriesDef("us_3m", "DTB3", "米3か月物国債（T-Bill）", "rates", "%", "daily"),
    SeriesDef("us_2y", "DGS2", "米2年国債利回り", "rates", "%", "daily"),
    SeriesDef("us_10y", "DGS10", "米10年国債利回り", "rates", "%", "daily"),
    SeriesDef("us_10y_real", "DFII10", "米10年実質金利（TIPS）", "rates", "%", "daily"),
    SeriesDef("us_10y_3m", "T10Y3M", "米10年−3か月スプレッド", "rates", "%pt", "daily", "マイナスは逆イールド"),
    SeriesDef("us_10y_2y", "T10Y2Y", "米10年−2年スプレッド", "rates", "%pt", "daily"),
    SeriesDef("us_baa_spread", "BAA10Y", "Baa社債−10年国債スプレッド", "rates", "%pt", "daily", "信用リスクの目安"),
    # インフレ
    SeriesDef("us_cpi", "CPIAUCSL", "米消費者物価指数（CPI）", "inflation", "指数", "monthly", "前年比は画面で計算"),
    SeriesDef("us_core_cpi", "CPILFESL", "米コアCPI（食品・エネルギー除く）", "inflation", "指数", "monthly"),
    SeriesDef("us_breakeven_10y", "T10YIE", "米10年期待インフレ率（BEI）", "inflation", "%", "daily"),
    # 雇用・景気
    SeriesDef("us_unemployment", "UNRATE", "米失業率", "labor", "%", "monthly"),
    SeriesDef("us_payrolls", "PAYEMS", "米非農業部門雇用者数", "labor", "千人", "monthly"),
    SeriesDef("us_indpro", "INDPRO", "米鉱工業生産指数", "activity", "指数", "monthly"),
    SeriesDef("us_real_gdp", "GDPC1", "米実質GDP", "activity", "10億ドル（年率）", "quarterly"),
    # 為替・商品・変動性
    SeriesDef("usdjpy", "DEXJPUS", "ドル円", "fx", "円/ドル", "daily", "ニューヨーク正午の為替レート"),
    SeriesDef("eurusd", "DEXUSEU", "ユーロドル", "fx", "ドル/ユーロ", "daily"),
    SeriesDef("usd_broad", "DTWEXBGS", "ドル指数（広義・名目）", "fx", "指数", "daily"),
    SeriesDef("wti", "DCOILWTICO", "WTI原油", "commodity", "ドル/バレル", "daily"),
    SeriesDef("vix", "VIXCLS", "VIX（S&P500の予想変動率）", "volatility", "指数", "daily"),
    # 日本
    SeriesDef("jp_10y", "IRLTLT01JPM156N", "日本の長期金利（10年、OECD）", "japan", "%", "monthly"),
    SeriesDef("jp_call", "IRSTCI01JPM156N", "日本のコールレート（OECD）", "japan", "%", "monthly"),
    # 株価指数（終値）
    SeriesDef("idx_sp500", "SP500", "S&P 500", "index", "ポイント", "daily", "FREDでは直近10年分のみ"),
    SeriesDef("idx_nasdaq", "NASDAQCOM", "NASDAQ総合", "index", "ポイント", "daily"),
    SeriesDef("idx_nikkei225", "NIKKEI225", "日経平均株価", "index", "円", "daily"),
    SeriesDef("idx_djia", "DJIA", "ダウ工業株30種平均", "index", "ドル", "daily"),
)
BY_KEY = {s.key: s for s in SERIES}
INDEX_KEYS = {"idx_sp500": "sp500", "idx_nasdaq": "nasdaq", "idx_nikkei225": "nikkei225", "idx_djia": "djia"}


def parse_csv(text: str) -> tuple[list[tuple[date, float]], int]:
    """FREDのCSVを (日付, 値) のリストにする。欠損（'.' や空）は除外し、その件数も返す。"""
    rows, missing = [], 0
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or header[0].lower() not in ("observation_date", "date"):
        raise ValueError(f"想定外のCSV形式：{header}")
    for r in reader:
        if len(r) < 2:
            continue
        v = r[1].strip()
        if v in ("", "."):
            missing += 1
            continue
        rows.append((datetime.strptime(r[0], "%Y-%m-%d").date(), float(v)))
    return rows, missing


def parse_api(payload: dict) -> tuple[list[tuple[date, float]], int]:
    """公式API（file_type=json）の observations を (日付, 値) にする。欠損は '.'。"""
    rows, missing = [], 0
    for o in payload.get("observations", []):
        v = str(o.get("value", "")).strip()
        if v in ("", "."):
            missing += 1
            continue
        rows.append((datetime.strptime(o["date"], "%Y-%m-%d").date(), float(v)))
    return rows, missing


class FredClient:
    def __init__(self, api_key: str = ""):
        self.api_key = api_key.strip()
        self.key_rejected: str | None = None  # キーが拒否された理由（拒否後は CSV で続行）
        # 公式APIの上限は120リクエスト/分。CSVは規定が明示されていないため、どちらも控えめに1秒1件
        # 注意：FREDは一部の独自User-Agentの接続を切るため、HTTPライブラリの標準のUser-Agentを使う
        self.http = RateLimitedClient(min_interval=1.0, timeout=30.0, retries=2)

    @property
    def mode(self) -> str:
        return "api" if self.api_key and not self.key_rejected else "csv"

    def fetch(self, fred_id: str, start: date | None = None) -> tuple[list[tuple[date, float]], int]:
        if self.mode == "api":
            params = {"series_id": fred_id, "api_key": self.api_key, "file_type": "json"}
            if start:
                params["observation_start"] = start.isoformat()
            r = self.http.get(API, params=params)
            if r.status_code in (400, 401, 403) and "api_key" in r.text:
                self.key_rejected = f"FRED_API_KEY が受け付けられませんでした（{r.status_code}）。公開CSVで取得を続けます"
            else:
                check(r)
                return parse_api(r.json())
        params = {"id": fred_id}
        if start:
            params["cosd"] = start.isoformat()
        r = self.http.get(BASE, params=params)
        check(r)
        return parse_csv(r.text)

    def close(self):
        self.http.close()
