"""J-Quants API V2（日本取引所グループ）。

仕様（2026-11 確認：https://jpx-jquants.com/ja/spec/migration-v1-v2）：
- ベースURL https://api.jquants.com 、認証はダッシュボードで発行したAPIキーを `x-api-key` ヘッダーで送る
  （V1のトークン方式は廃止。Authorization ヘッダーと同時に送ると 403）
- 応答は {"data": [...], "pagination_key": "..."}。pagination_key がある間は続きを取得する
- 株価四本値 /v2/equities/bars/daily（code か date が必須）。列名は短縮形（O,H,L,C,Vo,Va,AdjC など）
- TOPIX /v2/indices/bars/daily/topix 、上場銘柄一覧 /v2/equities/master 、財務情報 /v2/fins/summary
- レート制限（リクエスト/分）：Free 5、Light 60、Standard 120、Premium 500
- Freeプラン：データは約12週間遅延、過去2年分
"""
from datetime import date

from .http import RateLimitedClient, check

BASE = "https://api.jquants.com"
RATE_PER_MIN = {"free": 5, "light": 60, "standard": 120, "premium": 500}
DELAY_NOTE = {"free": "約12週間遅延（Freeプラン）", "light": "前営業日までのデータ（Lightプラン）",
              "standard": "前営業日までのデータ（Standardプラン）", "premium": "前営業日までのデータ（Premiumプラン）"}
DELAY_DAYS = {"free": 84}  # 12週間


class PlanNotAvailable(Exception):
    """契約プランでは提供されていないAPI（例：Freeプランの TOPIX）。認証の失敗とは区別する。"""


def to_jq_code(code: str) -> str:
    """JPXの4桁（英字を含む場合あり）のコードを、J-Quantsの5桁コードにする（末尾0は普通株式）。"""
    return code if len(code) == 5 else code + "0"


def from_jq_code(code: str) -> str:
    return code[:4] if len(code) == 5 and code.endswith("0") else code


def parse_daily_bars(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r.get("C") is None:  # 取引が成立しなかった日（四本値が Null）
            continue
        out.append({"code": from_jq_code(str(r["Code"])), "date": date.fromisoformat(r["Date"]),
                    "open": r.get("O"), "high": r.get("H"), "low": r.get("L"), "close": float(r["C"]),
                    "adj_close": r.get("AdjC"), "volume": r.get("Vo"), "turnover": r.get("Va"),
                    "market_cap_mn": r.get("MktCap"), "adj_factor": r.get("AdjFactor")})
    return out


def parse_index_bars(rows: list[dict]) -> list[dict]:
    return [{"date": date.fromisoformat(r["Date"]), "open": r.get("O"), "high": r.get("H"), "low": r.get("L"),
             "close": float(r["C"])} for r in rows if r.get("C") is not None]


class JQuantsClient:
    def __init__(self, api_key: str, plan: str = "free", network_secret: bool = False):
        if not api_key and not network_secret:
            raise ValueError("J-Quants のAPIキーが未設定です（JQUANTS_API_KEY）")
        per_min = RATE_PER_MIN.get(plan, 5)
        # ネットワークシークレットの場合、x-api-key はプロキシが付けるので、ここでは付けない（二重送信を避ける）
        headers = {} if network_secret else {"x-api-key": api_key}
        self.http = RateLimitedClient(min_interval=60.0 / per_min + 0.2, headers=headers)

    def _paged(self, path: str, params: dict) -> list[dict]:
        rows, p = [], dict(params)
        while True:
            r = self.http.get(BASE + path, params=p)
            if r.status_code == 403 and "not available on your subscription" in r.text:
                raise PlanNotAvailable(f"{path} はご契約のプランでは提供されていません")
            if r.status_code in (401, 403):
                raise PermissionError(f"J-Quants の認証に失敗しました（{r.status_code}）。APIキーとプランを確認してください")
            check(r)
            j = r.json()
            rows.extend(j.get("data", []))
            if not j.get("pagination_key"):
                return rows
            p["pagination_key"] = j["pagination_key"]

    def daily_bars_by_date(self, d: date) -> list[dict]:
        return self._paged("/v2/equities/bars/daily", {"date": d.strftime("%Y%m%d")})

    def daily_bars_by_code(self, code: str, start: date | None = None, end: date | None = None) -> list[dict]:
        p = {"code": to_jq_code(code)}
        if start:
            p["from"] = start.strftime("%Y%m%d")
        if end:
            p["to"] = end.strftime("%Y%m%d")
        return self._paged("/v2/equities/bars/daily", p)

    def topix(self, start: date | None = None) -> list[dict]:
        return self._paged("/v2/indices/bars/daily/topix", {"from": start.strftime("%Y%m%d")} if start else {})

    def close(self):
        self.http.close()
