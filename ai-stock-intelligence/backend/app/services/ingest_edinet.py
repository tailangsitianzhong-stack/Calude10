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
from ..models import DataQualityCheck, EdinetDocument, FinancialFact, IngestionRun, Security
from ..providers import edinet
from ..providers.http import redact

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


def cross_check(session: Session, sid: int, code: str, parsed: dict) -> list[dict]:
    """同じ期末の J-Quants の総資産・純資産と比べる。食い違いは品質の記録にする。"""
    out = []
    for concept in ("assets", "net_assets"):
        if concept not in parsed["values"]:
            continue
        jq = session.scalars(select(FinancialFact.value).where(
            FinancialFact.security_id == sid, FinancialFact.period_end == parsed["period_end"],
            FinancialFact.concept == concept, FinancialFact.tag.like("jquants:%"))).all()
        ev = parsed["values"][concept]["value"]
        for v in set(jq):
            ok = abs(v - ev) <= 1.0
            out.append({"concept": concept, "edinet": ev, "jquants": v, "match": ok})
            if not ok:
                session.add(DataQualityCheck(dataset=QUALITY, subject=code, check_type="inconsistent", severity="warn",
                                             detail={"concept": concept, "period_end": str(parsed["period_end"]),
                                                     "edinet": ev, "jquants": v}))
    return out


def run(session: Session, codes: list[str], start: date, end: date) -> IngestionRun:
    """codes（4桁の証券コード）について、start〜end に提出された決算書類を取得する。"""
    st = get_settings()
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
                        checks = cross_check(session, sec.id, sec.code, parsed)
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
                            session.add(DataQualityCheck(dataset=QUALITY, subject=sec.code, check_type="invalid_value",
                                                         severity="warn", detail={"doc_id": meta["docID"], **p}))
                        session.commit()
                        stats["documents_parsed"] += 1
                        stats["facts_added"] += added
                        stats["facts_existing"] += existing
                        stats["checks"] += len(checks)
                        stats["check_mismatch"] += sum(not c["match"] for c in checks)
                    except Exception as e:  # 1件の書類の失敗で他を止めない。失敗も記録して原因を追えるようにする
                        session.rollback()
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
