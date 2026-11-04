"""米国株の日次株価（差し替え可能な2つのアダプター）。

- Tiingo：https://api.tiingo.com/tiingo/daily/{ticker}/prices（Authorization: Token <key>）
  無料プランは 50リクエスト/時・1,000/日・月500銘柄、社内利用（Internal Use）のみ【2026-11時点の公開情報、要確認】
- Massive（旧 Polygon.io）：/v2/aggs/grouped/locale/us/market/stocks/{date}（全銘柄の1日分を1回で取得）
  接続先は公式ドキュメントの https://api.massive.com（旧 https://api.polygon.io も同じ応答）。認証は Authorization: Bearer <key>
  （公式：https://massive.com/docs/rest/quickstart）。無料の Stocks Basic：日足（終値ベース）・5リクエスト/分。
  全銘柄の日足（grouped daily）は「すべての株式プランで利用可能」（公式：.../aggregates/daily-market-summary）

どちらもキー未設定なら使わない（画面は「未接続」）。
"""
from datetime import date, datetime, timezone

from .http import RateLimitedClient, check


def parse_tiingo(ticker: str, rows: list[dict]) -> list[dict]:
    return [{"code": ticker, "date": date.fromisoformat(r["date"][:10]), "open": r.get("open"), "high": r.get("high"),
             "low": r.get("low"), "close": float(r["close"]), "adj_close": r.get("adjClose"), "volume": r.get("volume")}
            for r in rows if r.get("close") is not None]


def parse_massive_grouped(payload: dict) -> list[dict]:
    out = []
    for r in payload.get("results") or []:
        if r.get("c") is None or "T" not in r:
            continue
        d = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).date()
        out.append({"code": r["T"].replace("-", "."), "date": d, "open": r.get("o"), "high": r.get("h"), "low": r.get("l"),
                    "close": float(r["c"]), "adj_close": None, "volume": r.get("v")})
    return out


class TiingoClient:
    name = "tiingo"
    delay_note = "日足（米国東部時間17:30頃に更新）"

    def __init__(self, api_key: str, network_secret: bool = False):
        if not api_key and not network_secret:
            raise ValueError("Tiingo のAPIキーが未設定です（TIINGO_API_KEY）")
        # 無料プランの上限（50リクエスト/時）に合わせて72秒に1件。ネットワークシークレットならヘッダーはプロキシが付ける
        headers = {} if network_secret else {"Authorization": f"Token {api_key}"}
        self.http = RateLimitedClient(min_interval=72.0, headers=headers)

    def daily(self, ticker: str, start: date) -> list[dict]:
        r = self.http.get(f"https://api.tiingo.com/tiingo/daily/{ticker.replace('.', '-')}/prices",
                          params={"startDate": start.isoformat()})
        if r.status_code in (401, 403):
            raise PermissionError(f"Tiingo の認証に失敗しました（{r.status_code}）")
        if r.status_code == 404:
            return []
        check(r)
        return parse_tiingo(ticker, r.json())

    def close(self):
        self.http.close()


class MassiveClient:
    name = "massive"
    delay_note = "日足（無料プランは前営業日までの終値）"

    def __init__(self, api_key: str, base_url: str = "https://api.massive.com", network_secret: bool = False):
        if not api_key and not network_secret:
            raise ValueError("Massive（旧Polygon.io）のAPIキーが未設定です（MASSIVE_API_KEY）")
        self.base = base_url.rstrip("/")
        # キーはURLに含めず Authorization ヘッダーで送る（ログ・エラーに残さないため）。ネットワークシークレットならプロキシが付ける
        headers = {} if network_secret else {"Authorization": f"Bearer {api_key}"}
        self.http = RateLimitedClient(min_interval=12.5, headers=headers)  # 無料 5回/分

    def grouped_daily(self, d: date) -> list[dict]:
        r = self.http.get(f"{self.base}/v2/aggs/grouped/locale/us/market/stocks/{d.isoformat()}",
                          params={"adjusted": "true"})
        if r.status_code in (401, 403):
            raise PermissionError(f"Massive の認証・プランの範囲外です（{r.status_code}）")
        check(r)
        return parse_massive_grouped(r.json())

    def close(self):
        self.http.close()
