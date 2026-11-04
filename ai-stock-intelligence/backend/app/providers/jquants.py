"""J-Quants API V2（日本取引所グループ）。

仕様（2026-11 確認：https://jpx-jquants.com/ja/spec/migration-v1-v2）：
- ベースURL https://api.jquants.com 、認証はダッシュボードで発行したAPIキーを `x-api-key` ヘッダーで送る
  （V1のトークン方式は廃止。Authorization ヘッダーと同時に送ると 403）
- 応答は {"data": [...], "pagination_key": "..."}。pagination_key がある間は続きを取得する
- 株価四本値 /v2/equities/bars/daily（code か date が必須）。列名は短縮形（O,H,L,C,Vo,Va,AdjC など）
- TOPIX /v2/indices/bars/daily/topix 、上場銘柄一覧 /v2/equities/master 、財務情報 /v2/fins/summary
- レート制限（リクエスト/分）：Free 5、Light 60、Standard 120、Premium 500
- Freeプラン：データは約12週間遅延、過去2年分

財務情報 /v2/fins/summary（2026-11-04 公式仕様 https://jpx-jquants.com/ja/spec/fin-summary を確認）：
- code（4桁/5桁）か date（開示日）の指定が必須。応答の値はすべて文字列、値がない項目は空文字列
- 金額は円単位（決算短信の表示単位に関わらない）。損益・CFは期首からの累計（実データでも CurPerSt＝CurFYSt を確認）
- 項目名は日本基準が基準。IFRS・米国基準には経常利益（OdP）の概念がなく空欄
- NP は親会社株主に帰属する当期純利益。Eq は「純資産」（実データで、自己資本比率 EqAR より Eq/TA が大きい
  企業が多いことを確認＝非支配株主持分などを含む）。負債の合計の項目はない
- 会計基準・連結/個別は DocType に含まれる（例 FYFinancialStatements_Consolidated_IFRS）
- 訂正開示には新しい開示番号（DiscNo）が付き、既存のレコードは上書きされない。開示番号は開示順を保証しないため、
  順序は開示日・開示時刻（DiscDate・DiscTime）で決める
- プランの上限とは別に、このAPI専用の上限 60リクエスト/分（https://jpx-jquants.com/ja/spec/rate-limits）
- Freeプランで取得できるのは、開示日が12週間前〜2年12週間前のもの（https://jpx-jquants.com/ja/spec/data-spec）
"""
import re
from datetime import date

from .http import RateLimitedClient, check

BASE = "https://api.jquants.com"
RATE_PER_MIN = {"free": 5, "light": 60, "standard": 120, "premium": 500}
DELAY_NOTE = {"free": "約12週間遅延（Freeプラン）", "light": "前営業日までのデータ（Lightプラン）",
              "standard": "前営業日までのデータ（Standardプラン）", "premium": "前営業日までのデータ（Premiumプラン）"}
DELAY_DAYS = {"free": 84}  # 12週間


FINS_PER_MIN = 60  # /v2/fins/summary 専用の上限（プランに関わらない）


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
        # 上限に達した場合は1分待って再試行する（分単位の上限のため）
        self.http = RateLimitedClient(min_interval=60.0 / per_min + 0.2, headers=headers, retries=3, rate_limit_wait=61.0)
        # 財務情報は専用の上限（60/分）。429 には Retry-After が付かないため61秒待つ（公式仕様）
        self.fins_http = RateLimitedClient(min_interval=60.0 / FINS_PER_MIN + 0.1, headers=headers, retries=3,
                                           rate_limit_wait=61.0)

    def _paged(self, path: str, params: dict, http: RateLimitedClient | None = None) -> list[dict]:
        rows, p = [], dict(params)
        while True:
            r = (http or self.http).get(BASE + path, params=p)
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

    def fins_summary(self, code: str | None = None, disc_date: date | None = None) -> list[dict]:
        """財務情報（決算短信のサマリー）。code（銘柄の全期間）か disc_date（その日の全銘柄の開示）を指定する。"""
        if not code and not disc_date:
            raise ValueError("code か disc_date のどちらかが必要です")
        p = {}
        if code:
            p["code"] = to_jq_code(code)
        if disc_date:
            p["date"] = disc_date.strftime("%Y%m%d")
        return self._paged("/v2/fins/summary", p, self.fins_http)

    def close(self):
        self.http.close()
        self.fins_http.close()


# ---- 財務情報の変換 ----

# 公式の項目名 → (アプリ内の共通名, 単位, 種類)。種類：flow＝期首からの累計、instant＝期末時点、fy_only＝通期の書類だけ
# 純資産（Eq）は米国企業の株主資本（equity）と意味が違うため、別の共通名 net_assets にする
FIN_FIELDS: dict[str, tuple[str, str, str]] = {
    "Sales": ("revenue", "JPY", "flow"),
    "OP": ("operating_income", "JPY", "flow"),
    "OdP": ("ordinary_income", "JPY", "flow"),
    "NP": ("net_income", "JPY", "flow"),
    "EPS": ("eps_basic", "JPY/shares", "flow"),
    "DEPS": ("eps_diluted", "JPY/shares", "flow"),
    "CFO": ("cfo", "JPY", "flow"),
    "CFI": ("cfi", "JPY", "flow"),
    "CFF": ("cff", "JPY", "flow"),
    "AvgSh": ("shares_avg", "shares", "flow"),
    "TA": ("assets", "JPY", "instant"),
    "Eq": ("net_assets", "JPY", "instant"),
    "EqAR": ("equity_ratio", "pure", "instant"),
    "BPS": ("bps", "JPY/shares", "instant"),
    "CashEq": ("cash", "JPY", "instant"),
    "ShOutFY": ("shares_issued", "shares", "instant"),
    "TrShFY": ("treasury_shares", "shares", "instant"),
    "DivAnn": ("dividend_per_share", "JPY/shares", "fy_only"),
}
PERIOD_MAP = {"1Q": "Q1", "2Q": "Q2", "3Q": "Q3", "4Q": "Q4", "5Q": "Q5", "FY": "FY"}
STANDARDS = {"JP": "JGAAP", "IFRS": "IFRS", "US": "USGAAP", "JMIS": "JMIS", "Foreign": "Foreign", "REIT": "REIT"}
_DOC = re.compile(r"^(FY|1Q|2Q|3Q|OtherPeriod)FinancialStatements_(Consolidated|NonConsolidated)_(JP|IFRS|US|JMIS|Foreign|REIT)$")
FORECAST_DOCS = {"EarnForecastRevision", "DividendForecastRevision", "REITEarnForecastRevision", "REITDividendForecastRevision"}


def parse_doc_type(doc_type: str) -> dict | None:
    """開示書類種別（公式の一覧 https://jpx-jquants.com/ja/spec/fin-summary/typeofdocument）を分解する。
    決算短信なら {"kind": "statement", ...}、業績・配当予想の修正なら {"kind": "forecast"}、知らない種別は None。"""
    if doc_type in FORECAST_DOCS:
        return {"kind": "forecast", "consolidated": None, "standard": "REIT" if doc_type.startswith("REIT") else None}
    m = _DOC.match(doc_type or "")
    if not m:
        return None
    return {"kind": "statement", "consolidated": m.group(2) == "Consolidated", "standard": STANDARDS[m.group(3)]}


def to_number(v) -> float | None:
    """応答の値（文字列）を数値にする。空文字列は「記載なし」で None（推測で埋めない）。数値でない値は ValueError。"""
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    return float(s)


def form_code(period_type: str) -> str:
    """financial_facts.form（16文字まで）に入れる書類の種類。例 TANSHIN-FY、TANSHIN-Q1"""
    return "TANSHIN-" + PERIOD_MAP.get(period_type, period_type)[:8]


def parse_fin_record(r: dict) -> dict:
    """財務情報の1レコードを、開示の情報（disclosure）と保存する値（facts）、変換できなかった項目（problems）に分ける。"""
    doc = parse_doc_type(r.get("DocType", ""))
    disclosure = {
        "disc_no": r["DiscNo"], "jq_code": str(r["Code"]), "code": from_jq_code(str(r["Code"])),
        "code_matchable": len(str(r["Code"])) == 5 and str(r["Code"]).endswith("0"),
        "disc_date": date.fromisoformat(r["DiscDate"]), "disc_time": r.get("DiscTime") or None,
        "doc_type": r.get("DocType", ""), "kind": doc["kind"] if doc else "unknown",
        "accounting_standard": doc["standard"] if doc else None, "consolidated": doc["consolidated"] if doc else None,
        "period_type": r.get("CurPerType") or None,
        "period_start": date.fromisoformat(r["CurPerSt"]) if r.get("CurPerSt") else None,
        "period_end": date.fromisoformat(r["CurPerEn"]) if r.get("CurPerEn") else None,
        "fy_start": date.fromisoformat(r["CurFYSt"]) if r.get("CurFYSt") else None,
        "fy_end": date.fromisoformat(r["CurFYEn"]) if r.get("CurFYEn") else None,
        "raw": r,
    }
    facts, problems = [], []
    if not doc or doc["kind"] != "statement" or not disclosure["period_end"]:
        return {"disclosure": disclosure, "facts": facts, "problems": problems}
    is_fy = disclosure["period_type"] == "FY"
    for field, (concept, unit, kind) in FIN_FIELDS.items():
        try:
            v = to_number(r.get(field))
        except ValueError:
            problems.append({"field": field, "value": str(r.get(field))[:40], "reason": "数値に変換できない"})
            continue
        if v is None or (kind == "fy_only" and not is_fy):
            continue
        facts.append({"concept": concept, "tag": f"jquants:{field}", "unit": unit,
                      "period_start": None if kind == "instant" else disclosure["period_start"],
                      "period_end": disclosure["period_end"],
                      "fiscal_year": disclosure["fy_end"].year if disclosure["fy_end"] else None,
                      "fiscal_period": PERIOD_MAP.get(disclosure["period_type"], disclosure["period_type"]),
                      "form": form_code(disclosure["period_type"] or ""), "accn": disclosure["disc_no"],
                      "filed": disclosure["disc_date"], "value": v})
    return {"disclosure": disclosure, "facts": facts, "problems": problems}
