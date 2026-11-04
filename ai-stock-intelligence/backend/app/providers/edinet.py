"""EDINET API v2（金融庁）。日本企業の負債の合計など、J-Quants の決算短信のサマリーにない項目を補う。

仕様（2026-11-04 確認：EDINET API仕様書 Version 2
https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf 、
様式コードリスト https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140327.xlsx）：
- 書類一覧  GET https://api.edinet-fsa.go.jp/api/v2/documents.json?date=YYYY-MM-DD&type=2&Subscription-Key=…
- 書類取得  GET https://api.edinet-fsa.go.jp/api/v2/documents/{docID}?type=5&Subscription-Key=…（XBRLをCSVに変換したZIP）
- APIキーは URL のパラメータ（Subscription-Key）で送る。ヘッダーでは送れないため、URL をログ・エラーに出さない
- 対象の様式（府令コード 010）：有価証券報告書 030000（訂正 030001）、半期報告書 043A00（訂正 043A01）、
  四半期報告書 043000（訂正 043001。2024年の制度廃止前の書類）
- 書類一覧の periodStart/periodEnd は事業年度の期間（半期報告書でも年度）。期末日は CSV の DEI（CurrentPeriodEndDateDEI）で決める

CSV の形式（2026-11-04 に実際の書類で確認：トヨタ・キーエンス・三菱UFJ の有価証券報告書と半期報告書）：
- ZIP の中の XBRL_TO_CSV/jpcrp*.csv が本文（jpaud* は監査報告書）。UTF-16（BOMつき）・タブ区切り
- 列：要素ID、項目名、コンテキストID、相対年度、連結・個別、期間・時点、ユニットID、単位、値。金額は円単位
- 期末時点のコンテキスト：有価証券報告書 CurrentYearInstant、半期報告書 InterimInstant
  （旧様式の第2四半期の書類は CurrentQuarterInstant）。個別の値は末尾が _NonConsolidatedMember
- IFRS の連結の値は「連結・個別」の列が「その他」になるため、列ではなくコンテキストIDで連結を判定する
"""
import csv
import io
import re
import zipfile
from datetime import date

from .http import RateLimitedClient, check

BASE = "https://api.edinet-fsa.go.jp/api/v2"
FORMS = {"030000": ("ASR", "有価証券報告書"), "030001": ("ASR", "訂正有価証券報告書"),
         "043A00": ("SSR", "半期報告書"), "043A01": ("SSR", "訂正半期報告書"),
         "043000": ("QR", "四半期報告書"), "043001": ("QR", "訂正四半期報告書")}
ORDINANCE = "010"  # 企業内容等の開示に関する内閣府令
PERIOD_CONTEXTS = ("CurrentYearInstant", "InterimInstant", "CurrentQuarterInstant")
# アプリ内の共通名 → 要素ID（日本基準、IFRS）。報告された値だけを使う（計算で補わない）
ELEMENTS: dict[str, tuple[str, ...]] = {
    "liabilities": ("jppfs_cor:Liabilities", "jpigp_cor:LiabilitiesIFRS"),
    "assets": ("jppfs_cor:Assets", "jpigp_cor:AssetsIFRS"),
    "net_assets": ("jppfs_cor:NetAssets", "jpigp_cor:EquityIFRS"),
}
STANDARDS = {"Japan GAAP": "JGAAP", "IFRS": "IFRS", "US GAAP": "USGAAP", "JMIS": "JMIS"}


class EdinetAuthError(PermissionError):
    pass


def json_status(j) -> str:
    """EDINET の JSON 応答の状態コード（書類一覧は metadata.status、エラーは StatusCode／statusCode など）。不明なら ""。
    認証の拒否が HTTP 200 の本文で返る場合に備えて、本文の状態コードも確認する（実際の応答形式は未確認のため防御的に）。"""
    if not isinstance(j, dict):
        return ""
    meta = j.get("metadata") if isinstance(j.get("metadata"), dict) else {}
    for v in (meta.get("status"), j.get("StatusCode"), j.get("statusCode"), j.get("status")):
        if v not in (None, ""):
            return str(v).strip()
    return ""


class EdinetClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("EDINET のAPIキーが未設定です（EDINET_API_KEY）")
        self._key = api_key
        # 公式の上限は公表されていない。1秒に1件に抑える
        self.http = RateLimitedClient(min_interval=1.0, headers={"User-Agent": "AI-STOCK-INTELLIGENCE"}, retries=3)

    def _get(self, path: str, params: dict):
        r = self.http.get(BASE + path, params={**params, "Subscription-Key": self._key})
        if r.status_code == 401:
            raise EdinetAuthError("EDINET の認証に失敗しました（401）。EDINET_API_KEY を確認してください")
        check(r)  # エラーのURLはキーを伏せる（http.redact）
        return r

    def documents(self, d: date) -> list[dict]:
        j = self._get("/documents.json", {"date": d.isoformat(), "type": 2}).json()
        status = json_status(j)
        if status == "401":
            raise EdinetAuthError("EDINET の認証に失敗しました（401）")
        if status != "200":
            raise RuntimeError(f"EDINET 書類一覧の応答が異常です（status {status}）")
        return j.get("results") or []

    def csv_package(self, doc_id: str) -> bytes:
        r = self._get(f"/documents/{doc_id}", {"type": 5})
        if "json" in r.headers.get("Content-Type", ""):  # エラーは JSON、成功は ZIP（公式仕様）
            try:
                status = json_status(r.json())
            except ValueError:
                status = ""
            if status == "401":  # 本文で認証の拒否を示す場合も、認証エラーとして実行全体を止める
                raise EdinetAuthError("EDINET の認証に失敗しました（401）")
            raise RuntimeError(f"EDINET 書類取得のエラー応答（{doc_id}、status {status or '不明'}）")
        return r.content

    def close(self):
        self.http.close()


def periodic_documents(results: list[dict], sec_codes: set[str] | None = None) -> list[dict]:
    """書類一覧から、対象の決算書類（取下げ済みを除く）を選ぶ。sec_codes は5桁の証券コード。"""
    out = []
    for x in results:
        if x.get("ordinanceCode") != ORDINANCE or x.get("formCode") not in FORMS:
            continue
        if not x.get("secCode") or (sec_codes is not None and x["secCode"] not in sec_codes):
            continue
        if str(x.get("withdrawalStatus", "0")) != "0":
            continue
        out.append(x)
    return out


def parse_csv_package(data: bytes) -> dict:
    """CSV の ZIP から、DEI（会計基準・期末日など）と、期末時点の連結の値を取り出す。"""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = [n for n in z.namelist() if re.search(r"(^|/)jpcrp[^/]*\.csv$", n)]
    if not names:
        raise ValueError("本文の CSV（jpcrp）がない")
    rows = list(csv.reader(io.StringIO(z.read(names[0]).decode("utf-16")), delimiter="\t"))
    if not rows or rows[0][:3] != ["要素ID", "項目名", "コンテキストID"]:
        raise ValueError("CSV の列の形式が想定と違う")
    rows = rows[1:]
    dei = {r[0].split(":", 1)[1]: r[8] for r in rows if r[0].startswith("jpdei_cor:") and len(r) >= 9}
    consolidated = dei.get("WhetherConsolidatedFinancialStatementsArePreparedDEI") == "true"
    # 連結財務諸表を作らない会社は、個別の値（_NonConsolidatedMember）を使う（実データでは未検証）
    contexts = PERIOD_CONTEXTS if consolidated else tuple(c + "_NonConsolidatedMember" for c in PERIOD_CONTEXTS)
    values, problems = {}, []
    for concept, elements in ELEMENTS.items():
        for ctx in contexts:
            hit = [r for r in rows if r[0] in elements and r[2] == ctx and len(r) >= 9]
            if hit:
                r = hit[0]
                if r[6] != "JPY":
                    problems.append({"concept": concept, "reason": f"単位が円ではない（{r[6]}）"})
                    break
                try:
                    values[concept] = {"value": float(r[8]), "tag": f"edinet:{r[0]}", "context": ctx}
                except ValueError:
                    problems.append({"concept": concept, "reason": "数値に変換できない", "value": r[8][:40]})
                break
    pe = (dei.get("CurrentPeriodEndDateDEI") or "").strip()
    if not pe:  # 期末日がなければ、どの期の値か決められない：読み取りのエラーにする（保存の段階に進めない）
        raise ValueError("期末日（CurrentPeriodEndDateDEI）がない")
    return {"period_end": date.fromisoformat(pe),
            "fy_start": date.fromisoformat(dei["CurrentFiscalYearStartDateDEI"]) if dei.get("CurrentFiscalYearStartDateDEI") else None,
            "fy_end": date.fromisoformat(dei["CurrentFiscalYearEndDateDEI"]) if dei.get("CurrentFiscalYearEndDateDEI") else None,
            "period_type": dei.get("TypeOfCurrentPeriodDEI"),
            "accounting_standard": STANDARDS.get(dei.get("AccountingStandardsDEI", ""), dei.get("AccountingStandardsDEI")),
            "consolidated": consolidated,
            "sec_code": dei.get("SecurityCodeDEI"), "values": values, "problems": problems, "csv_name": names[0]}
