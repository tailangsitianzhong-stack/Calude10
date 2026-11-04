"""EDINET から日本企業の貸借対照表の値（負債の合計など）を取得して保存する（EDINET_API_KEY 設定時のみ）。

- 指定した銘柄・期間の書類一覧（1日1リクエスト）から決算書類を探し、CSV（type=5）を読む。
- 値は financial_facts に追記する（form は EDINET-ASR／EDINET-SSR／EDINET-QR、accn は書類管理番号、
  filed は EDINET への提出日）。既存の行は更新・削除しない。日本株の銘柄にだけ書き込む。
- 同じ期末の J-Quants（決算短信）の値と食い違えば、品質の記録（inconsistent）に残す。
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import DataQualityCheck, EdinetDocument, FetchLog, FinancialFact, IngestionRun, JpDisclosure, Security
from ..providers import edinet
from ..providers.http import redact
from .quality import add_once, forget_pending

DATASET = "financials_jp_edinet"
QUALITY = "edinet"
SOURCE = "EDINET"


def store_document(session: Session, meta: dict, parsed: dict, sid: int, now: datetime) -> tuple[int, int]:
    """書類から取り出した値を保存する。戻り値は（追加した件数, 既存のため追加しなかった件数）。"""
    kind = edinet.FORMS[meta["formCode"]][0]
    filed = date.fromisoformat(meta["submitDateTime"][:10])
    period_type = "FY" if kind == "ASR" else "HY" if kind == "SSR" else (parsed.get("period_type") or "Q")
    have = {(t, pe) for t, pe in session.execute(select(FinancialFact.tag, FinancialFact.period_end)
                                                 .where(FinancialFact.security_id == sid, FinancialFact.accn == meta["docID"]))}
    added = existing = 0
    for concept, v in parsed["values"].items():
        if (v["tag"], parsed["period_end"]) in have:
            existing += 1
            continue
        session.add(FinancialFact(security_id=sid, concept=concept, tag=v["tag"], unit="JPY", period_start=None,
                                  period_end=parsed["period_end"],
                                  fiscal_year=parsed["fy_end"].year if parsed.get("fy_end") else None,
                                  fiscal_period=period_type, form=f"EDINET-{kind}", accn=meta["docID"], filed=filed,
                                  value=v["value"], source=SOURCE, fetched_at=now))
        added += 1
    return added, existing


ROUNDING_YEN = 1_000_000  # 決算短信は百万円未満を切り捨てた値のことがある（実データ：4263 の総資産 4,218,107,000円 → 4,218,000,000円）


def cross_check(session: Session, sid: int, code: str, period_end: date, values: dict, standard: str | None,
                record: bool = True) -> list[dict]:
    """同じ期末・同じ会計基準の J-Quants（決算短信）の総資産・純資産と比べる（基準の違う決算短信とは比べない）。
    差が百万円未満なら「表示単位の丸め」として一致扱い。それ以上の食い違いは品質の記録（inconsistent）にする。"""
    out = []
    for concept in ("assets", "net_assets"):
        if concept not in values:
            continue
        rows = session.execute(
            select(FinancialFact.value, FinancialFact.accn, JpDisclosure.disc_date, JpDisclosure.disc_time)
            .join(JpDisclosure, JpDisclosure.disc_no == FinancialFact.accn)
            .where(FinancialFact.security_id == sid, FinancialFact.period_end == period_end, FinancialFact.concept == concept,
                   FinancialFact.tag.like("jquants:%"), JpDisclosure.accounting_standard == standard)).all()
        if not rows:
            continue
        v, accn, *_ = max(rows, key=lambda r: (r[2], r[3] or ""))  # 同じ基準の最新の決算短信
        ev = values[concept]["value"]
        diff = abs(v - ev)
        result = "一致" if diff <= 1.0 else ("表示単位の丸め" if diff < ROUNDING_YEN else "不一致")
        out.append({"concept": concept, "edinet": ev, "jquants": v, "jquants_disclosure": accn, "result": result,
                    "match": result != "不一致"})
        if result == "不一致" and record:
            session.add(DataQualityCheck(dataset=QUALITY, subject=code, check_type="inconsistent", severity="warn",
                                         detail={"concept": concept, "period_end": str(period_end), "standard": standard,
                                                 "edinet": ev, "jquants": v, "jquants_disclosure": accn}))
    return out


UNAVAILABLE_REASON = {
    "USGAAP": "米国基準の会社は、EDINET の CSV に負債の合計の要素がない（経営指標等の総資産・株主資本だけ）。計算では補わない",
}


def record_unavailable(session: Session, code: str, doc_id: str, standard: str | None, values: dict) -> bool:
    """書類から負債の合計を取り出せなかったことを品質の記録に残す（既存の値は変えない）。"""
    if "liabilities" in values:
        return False
    session.add(DataQualityCheck(dataset=QUALITY, subject=code, check_type="liabilities_unavailable", severity="info",
                                 detail={"doc_id": doc_id, "standard": standard,
                                         "reason": UNAVAILABLE_REASON.get(standard or "", "負債の合計の要素が見つからない")}))
    return True


def recheck(session: Session) -> dict:
    """保存済みの EDINET の値から、照合と「負債を取り出せなかった」の品質の記録を作り直す（API には接続しない）。
    財務の値（financial_facts）は変更しない。品質の記録（dataset=edinet の inconsistent・liabilities_unavailable）だけを置き換える。"""
    from sqlalchemy import delete
    session.execute(delete(DataQualityCheck).where(DataQualityCheck.dataset == QUALITY,
                                                   DataQualityCheck.check_type.in_(("inconsistent", "liabilities_unavailable"))))
    stats = {"documents": 0, "checks": 0, "mismatch": 0, "rounding": 0, "unavailable": 0}
    for doc in session.scalars(select(EdinetDocument).where(EdinetDocument.parse_status == "parsed")):
        code = doc.sec_code[:4]
        vals = {f.concept: {"value": f.value} for f in session.scalars(
            select(FinancialFact).where(FinancialFact.accn == doc.doc_id, FinancialFact.source == SOURCE))}
        checks = cross_check(session, doc.security_id, code, doc.period_end, vals, doc.accounting_standard)
        stats["documents"] += 1
        stats["checks"] += len(checks)
        stats["mismatch"] += sum(c["result"] == "不一致" for c in checks)
        stats["rounding"] += sum(c["result"] == "表示単位の丸め" for c in checks)
        stats["unavailable"] += record_unavailable(session, code, doc.doc_id, doc.accounting_standard, vals)
        doc.detail = {**(doc.detail or {}), "cross_check": checks}
    session.commit()
    return stats


LIST_DATASET = "edinet_list"  # 全銘柄（日本の普通株すべて）を対象に、書類一覧を処理し終えた提出日
DEFAULT_MAX_DOCS = 200  # 全銘柄を対象にするときの、1回（1バッチ）あたりの書類の取得数の上限（指定がない場合）


def _sec5(code: str) -> str:
    """銘柄一覧の4桁（英字を含む場合あり）を、EDINET の5桁の証券コードにする（末尾0）。"""
    return code + "0" if len(code) == 4 else code


def _process_document(session: Session, client, meta: dict, sec: Security, stats: dict, errors: list[str]) -> str:
    """1件の書類を読み取って保存する。戻り値は parsed / failed / skipped。失敗も記録し、次の実行で再試行する。"""
    prev = session.get(EdinetDocument, meta["docID"])
    if prev and prev.parse_status == "parsed":  # 読み取り済み
        stats["documents_skipped_existing"] += 1
        return "skipped"
    if prev:  # 前回失敗した書類の記録は、結果で置き換える（財務の値の行ではない）
        session.delete(prev)
        session.flush()
    now = datetime.now(timezone.utc)
    keep = {k: meta.get(k) for k in ("edinetCode", "filerName", "formCode", "docTypeCode", "docDescription",
                                     "periodStart", "periodEnd", "submitDateTime", "parentDocID", "csvFlag")}
    try:
        parsed = edinet.parse_csv_package(client.csv_package(meta["docID"]))
        added, existing = store_document(session, meta, parsed, sec.id, now)
        checks = cross_check(session, sec.id, sec.code, parsed["period_end"], parsed["values"], parsed["accounting_standard"])
        record_unavailable(session, sec.code, meta["docID"], parsed["accounting_standard"], parsed["values"])
        session.add(EdinetDocument(
            doc_id=meta["docID"], security_id=sec.id, sec_code=meta["secCode"], edinet_code=meta.get("edinetCode"),
            filer_name=meta.get("filerName"), form_code=meta["formCode"], doc_description=meta.get("docDescription"),
            submitted_at=meta["submitDateTime"], period_end=parsed["period_end"],
            accounting_standard=parsed["accounting_standard"], parse_status="parsed",
            detail={"meta": keep, "tags": {k: v["tag"] for k, v in parsed["values"].items()},
                    "contexts": {k: v["context"] for k, v in parsed["values"].items()},
                    "problems": parsed["problems"], "cross_check": checks, "csv": parsed["csv_name"]},
            fetched_at=now))
        for p in parsed["problems"]:
            add_once(session, QUALITY, sec.code, "invalid_value", {"doc_id": meta["docID"], "concept": p["concept"]},
                     "warn", {"doc_id": meta["docID"], **p})
        session.commit()
        stats["documents_parsed"] += 1
        stats["facts_added"] += added
        stats["facts_existing"] += existing
        stats["checks"] += len(checks)
        stats["check_mismatch"] += sum(not c["match"] for c in checks)
        return "parsed"
    except Exception as e:  # 1件の書類の失敗で他を止めない。失敗も記録して原因を追えるようにする
        session.rollback()
        forget_pending(session)
        errors.append(f"{meta['docID']}: {redact(str(e))[:150]}")
        session.add(EdinetDocument(
            doc_id=meta["docID"], security_id=sec.id, sec_code=meta["secCode"], edinet_code=meta.get("edinetCode"),
            filer_name=meta.get("filerName"), form_code=meta["formCode"], doc_description=meta.get("docDescription"),
            submitted_at=meta["submitDateTime"], period_end=None, accounting_standard=None, parse_status="failed",
            detail={"meta": keep, "error": redact(str(e))[:300]}, fetched_at=now))
        session.commit()
        stats["documents_failed"] += 1
        return "failed"


def run(session: Session, codes: list[str] | None = None, start: date | None = None, end: date | None = None,
        recheck_only: bool = False, all_listed: bool = False, max_docs: int | None = None) -> IngestionRun:
    """start〜end に提出された決算書類を取得する。

    - codes（4桁の証券コード）を指定するか、all_listed=True で日本の普通株すべて（上場中）を対象にする。
    - max_docs：1回（1バッチ）あたりの書類の取得数の上限。all_listed のときの既定は DEFAULT_MAX_DOCS。
    - all_listed のときは、書類一覧を処理し終えた提出日を fetch_log（edinet_list）に記録し、次の実行では取り直さない。
      上限に達して途中で止まった提出日は記録しないので、次の実行でその日の残りから続ける（読み取り済みはスキップ）。
    - 前回失敗した書類は、書類一覧を取り直さずに、保存しておいたメタデータから先に再試行する。
    - recheck_only なら API に接続せず、保存済みの値から品質の記録を作り直す。"""
    st = get_settings()
    if recheck_only:
        stats = recheck(session)
        r = IngestionRun(dataset=DATASET, source="保存済みの EDINET の値の再照合（API接続なし）", status="success", rows=0,
                         message=str(stats), finished_at=datetime.now(timezone.utc))
        session.add(r)
        session.commit()
        return r
    if all_listed and max_docs is None:
        max_docs = DEFAULT_MAX_DOCS
    scope = "日本の普通株すべて" if all_listed else f"{len(codes or [])}社"
    run = IngestionRun(dataset=DATASET, source=f"EDINET API v2（書類一覧・CSV、{scope}、上限 {max_docs or 'なし'} 件）",
                       status="running")
    session.add(run)
    session.commit()
    if not st.edinet_api_key.strip():
        run.status, run.message, run.finished_at = "skipped", "EDINET_API_KEY が未設定", datetime.now(timezone.utc)
        session.commit()
        return run
    q = select(Security).where(Security.market == "JP")
    q = q.where(Security.security_type == "common", Security.is_active.is_(True)) if all_listed else q.where(Security.code.in_(codes or []))
    targets = {_sec5(s.code): s for s in session.scalars(q)}
    client = edinet.EdinetClient(st.edinet_api_key.strip())
    stats = {"targets": len(targets), "list_requests": 0, "dates_skipped_listed": 0, "documents_found": 0,
             "documents_parsed": 0, "documents_failed": 0, "documents_retried": 0, "documents_skipped_existing": 0,
             "facts_added": 0, "facts_existing": 0, "checks": 0, "check_mismatch": 0, "limit_reached": False}
    errors: list[str] = []
    downloads = 0

    def room() -> bool:
        return max_docs is None or downloads < max_docs

    try:
        # 1) 前回失敗した書類を、保存しておいたメタデータから再試行する（書類一覧は取り直さない）
        failed = list(session.scalars(select(EdinetDocument).where(EdinetDocument.parse_status == "failed",
                                                                    EdinetDocument.sec_code.in_(list(targets)))
                                      .order_by(EdinetDocument.submitted_at)))
        for doc in failed:
            if not room():
                stats["limit_reached"] = True
                break
            meta = {**(doc.detail or {}).get("meta", {}), "docID": doc.doc_id, "secCode": doc.sec_code,
                    "formCode": doc.form_code, "submitDateTime": doc.submitted_at}
            _process_document(session, client, meta, targets[doc.sec_code], stats, errors)
            stats["documents_retried"] += 1
            downloads += 1
        # 2) 提出日ごとに書類一覧を取得し、対象の決算書類を読み取る
        listed = {k for (k,) in session.execute(select(FetchLog.key).where(FetchLog.dataset == LIST_DATASET))} if all_listed else set()
        d = start
        while d <= end and not stats["limit_reached"]:
            if d.weekday() < 5:
                if d.isoformat() in listed:
                    stats["dates_skipped_listed"] += 1
                    d += timedelta(days=1)
                    continue
                try:
                    docs = edinet.periodic_documents(client.documents(d), set(targets))
                    stats["list_requests"] += 1
                except edinet.EdinetAuthError as e:
                    errors.append(str(e))
                    break
                except Exception as e:  # この日は記録しないので、次の実行で取り直す
                    errors.append(f"{d}: {redact(str(e))[:150]}")
                    d += timedelta(days=1)
                    continue
                complete = True
                for meta in docs:
                    stats["documents_found"] += 1
                    prev = session.get(EdinetDocument, meta["docID"])
                    if prev and prev.parse_status == "parsed":
                        stats["documents_skipped_existing"] += 1
                        continue
                    if not room():  # 上限：この日の残りは次の実行で（この日は「処理済み」にしない）
                        stats["limit_reached"] = True
                        complete = False
                        break
                    _process_document(session, client, meta, targets[meta["secCode"]], stats, errors)
                    downloads += 1  # 失敗した書類も記録済みで、次の実行の 1) で再試行される
                if complete and all_listed:
                    log = session.scalar(select(FetchLog).where(FetchLog.dataset == LIST_DATASET, FetchLog.key == d.isoformat()))
                    if log is None:
                        session.add(FetchLog(dataset=LIST_DATASET, key=d.isoformat(), status="success", rows=len(docs),
                                             fetched_at=datetime.now(timezone.utc)))
                    session.commit()
            d += timedelta(days=1)
    finally:
        client.close()
    run = session.get(IngestionRun, run.id)
    run.rows, run.finished_at = stats["facts_added"], datetime.now(timezone.utc)
    run.status = "success" if not errors else ("partial" if stats["documents_parsed"] else "failed")
    run.message = ("; ".join([str(stats)] + errors[:5]))[:1000]
    session.commit()
    return run
