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
from ..models import DataQualityCheck, EdinetDocument, FinancialFact, IngestionRun, JpDisclosure, Security
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


def run(session: Session, codes: list[str] | None = None, start: date | None = None, end: date | None = None,
        recheck_only: bool = False) -> IngestionRun:
    """codes（4桁の証券コード）について、start〜end に提出された決算書類を取得する。
    recheck_only なら API に接続せず、保存済みの値から品質の記録を作り直す。"""
    st = get_settings()
    if recheck_only:
        stats = recheck(session)
        r = IngestionRun(dataset=DATASET, source="保存済みの EDINET の値の再照合（API接続なし）", status="success", rows=0,
                         message=str(stats), finished_at=datetime.now(timezone.utc))
        session.add(r)
        session.commit()
        return r
    run = IngestionRun(dataset=DATASET, source="EDINET API v2（書類一覧・CSV）", status="running")
    session.add(run)
    session.commit()
    if not st.edinet_api_key.strip():
        run.status, run.message, run.finished_at = "skipped", "EDINET_API_KEY が未設定", datetime.now(timezone.utc)
        session.commit()
        return run
    secs = {s.code: s for s in session.scalars(select(Security).where(Security.market == "JP", Security.code.in_(codes)))}
    targets = {c + "0": secs[c] for c in codes if c in secs}  # EDINET の証券コードは5桁（末尾0）
    client = edinet.EdinetClient(st.edinet_api_key.strip())
    stats = {"list_requests": 0, "documents_found": 0, "documents_parsed": 0, "documents_skipped_existing": 0,
             "facts_added": 0, "facts_existing": 0, "checks": 0, "check_mismatch": 0}
    errors: list[str] = []
    try:
        d = start
        while d <= end:
            if d.weekday() < 5:
                try:
                    docs = edinet.periodic_documents(client.documents(d), set(targets))
                    stats["list_requests"] += 1
                except edinet.EdinetAuthError as e:
                    errors.append(str(e))
                    break
                except Exception as e:
                    errors.append(f"{d}: {redact(str(e))[:150]}")
                    d += timedelta(days=1)
                    continue
                for meta in docs:
                    stats["documents_found"] += 1
                    sec = targets[meta["secCode"]]
                    prev = session.get(EdinetDocument, meta["docID"])
                    if prev and prev.parse_status == "parsed":  # 読み取り済み。失敗した書類は次の実行で再試行する
                        stats["documents_skipped_existing"] += 1
                        continue
                    if prev:  # 前回失敗した書類の記録は、結果で置き換える（財務の値の行ではない）
                        session.delete(prev)
                        session.flush()
                    now = datetime.now(timezone.utc)
                    keep = {k: meta.get(k) for k in ("edinetCode", "filerName", "formCode", "docTypeCode", "docDescription",
                                                     "periodStart", "periodEnd", "submitDateTime", "parentDocID", "csvFlag")}
                    try:
                        parsed = edinet.parse_csv_package(client.csv_package(meta["docID"]))
                        added, existing = store_document(session, meta, parsed, sec.id, now)
                        checks = cross_check(session, sec.id, sec.code, parsed["period_end"], parsed["values"],
                                             parsed["accounting_standard"])
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
            d += timedelta(days=1)
    finally:
        client.close()
    run = session.get(IngestionRun, run.id)
    run.rows, run.finished_at = stats["facts_added"], datetime.now(timezone.utc)
    run.status = "success" if not errors else ("partial" if stats["documents_parsed"] else "failed")
    run.message = ("; ".join([str(stats)] + errors[:5]))[:1000]
    session.commit()
    return run
