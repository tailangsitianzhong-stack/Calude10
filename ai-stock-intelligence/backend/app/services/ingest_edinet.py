"""EDINET から日本企業の貸借対照表の値（負債の合計など）を取得して保存する（EDINET_API_KEY 設定時のみ）。

- 指定した銘柄・期間の書類一覧（1日1リクエスト）から決算書類を探し、CSV（type=5）を読む。
- 値は financial_facts に追記する（form は EDINET-ASR／EDINET-SSR／EDINET-QR、accn は書類管理番号、
  filed は EDINET への提出日）。既存の行は更新・削除しない。日本株の銘柄にだけ書き込む。
- 同じ期末の J-Quants（決算短信）の値と食い違えば、品質の記録（inconsistent）に残す。
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
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
                record: bool = True, edinet_doc_id: str | None = None, edinet_submitted_at: str | None = None) -> list[dict]:
    """同じ期末・同じ会計基準の J-Quants（決算短信）の総資産・純資産と比べる（基準の違う決算短信とは比べない）。
    差が百万円未満なら「表示単位の丸め」として一致扱い。それ以上の食い違いは品質の記録（inconsistent）にする。
    記録には両方の出典を残す：決算短信の開示番号（jquants_disclosure）と、EDINET の書類管理番号・提出日
    （edinet_doc_id・edinet_submitted_at。2026-11-04 追加。これより前の記録にはこの2項目がない）。"""
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
                                                 "edinet": ev, "jquants": v, "jquants_disclosure": accn,
                                                 "edinet_doc_id": edinet_doc_id, "edinet_submitted_at": edinet_submitted_at}))
    return out


UNAVAILABLE_REASON = {
    "USGAAP": "米国基準の会社は、EDINET の CSV に負債の合計の要素がない（経営指標等の総資産・株主資本だけ）。計算では補わない",
}


def unavailable_type(standard: str | None) -> str:
    """負債を取り出せなかった理由の種類：us_gaap（米国基準のため取得不可）／no_liabilities_element（負債の要素がない）。"""
    return "us_gaap" if standard == "USGAAP" else "no_liabilities_element"


def record_unavailable(session: Session, code: str, doc_id: str, standard: str | None, values: dict) -> str | None:
    """書類から負債の合計を取り出せなかったことを品質の記録に残す（既存の値は変えない）。取得の失敗ではない。
    戻り値は理由の種類（unavailable_type）。負債を取り出せたときは None。"""
    if "liabilities" in values:
        return None
    kind = unavailable_type(standard)
    session.add(DataQualityCheck(dataset=QUALITY, subject=code, check_type="liabilities_unavailable", severity="info",
                                 detail={"doc_id": doc_id, "standard": standard, "reason_type": kind,
                                         "reason": UNAVAILABLE_REASON.get(standard or "", "負債の合計の要素が見つからない")}))
    return kind


def recheck(session: Session) -> dict:
    """保存済みの EDINET の値から、照合と「負債を取り出せなかった」の品質の記録を作り直す（API には接続しない）。
    財務の値（financial_facts）は変更しない。品質の記録（dataset=edinet の inconsistent・liabilities_unavailable）だけを置き換える。"""
    from sqlalchemy import delete
    session.execute(delete(DataQualityCheck).where(DataQualityCheck.dataset == QUALITY,
                                                   DataQualityCheck.check_type.in_(("inconsistent", "liabilities_unavailable"))))
    stats = {"documents": 0, "checks": 0, "mismatch": 0, "rounding": 0, "unavailable": 0,
             "unavailable_no_liabilities_element": 0, "unavailable_us_gaap": 0}
    for doc in session.scalars(select(EdinetDocument).where(EdinetDocument.parse_status == "parsed")):
        code = doc.sec_code[:4]
        vals = {f.concept: {"value": f.value} for f in session.scalars(
            select(FinancialFact).where(FinancialFact.accn == doc.doc_id, FinancialFact.source == SOURCE))}
        checks = cross_check(session, doc.security_id, code, doc.period_end, vals, doc.accounting_standard,
                             edinet_doc_id=doc.doc_id, edinet_submitted_at=doc.submitted_at)
        stats["documents"] += 1
        stats["checks"] += len(checks)
        stats["mismatch"] += sum(c["result"] == "不一致" for c in checks)
        stats["rounding"] += sum(c["result"] == "表示単位の丸め" for c in checks)
        kind = record_unavailable(session, code, doc.doc_id, doc.accounting_standard, vals)
        if kind:
            stats["unavailable"] += 1
            stats[f"unavailable_{kind}"] += 1
        doc.detail = {**(doc.detail or {}), "cross_check": checks}
    session.commit()
    return stats


LIST_DATASET = "edinet_list"  # 全銘柄（日本の普通株すべて）を対象に、書類一覧を処理し終えた提出日
MISMATCH_STOP_RATE = 0.10  # 不一致率がこれを超えたら自動停止する（バッチ単位・累計のどちらか）
MISMATCH_MIN_DOCS = 100  # 照合できた書類がこの件数を超えてから判定する（少ない件数ではぶれが大きい）


def compared_and_mismatch(checks: list[dict]) -> tuple[int, int]:
    """1件の書類の照合結果を（照合できたか, 不一致があったか）の 0/1 にする。
    照合できた＝同じ期末・同じ会計基準の決算短信と、総資産か純資産を比べられた。不一致＝1項目以上が「不一致」。"""
    return (1 if checks else 0), (1 if any(c.get("result") == "不一致" for c in checks) else 0)


def mismatch_totals(session: Session) -> tuple[int, int]:
    """保存済みの全書類（読み取り済み）の（照合できた書類数, 不一致のあった書類数）。累計の不一致率の元。"""
    n = m = 0
    for (detail,) in session.execute(select(EdinetDocument.detail).where(EdinetDocument.parse_status == "parsed")):
        c, x = compared_and_mismatch((detail or {}).get("cross_check") or [])
        n, m = n + c, m + x
    return n, m


def mismatch_exceeded(compared: int, mismatched: int, rate: float) -> bool:
    return compared > MISMATCH_MIN_DOCS and mismatched / compared > rate


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
    stage = "api"  # 失敗の種類：api（取得の通信・応答のエラー）／parse（書類の読み取りのエラー）／store（保存・照合の内部エラー）
    try:
        package = client.csv_package(meta["docID"])
        stage = "parse"
        parsed = edinet.parse_csv_package(package)
        stage = "store"
        added, existing = store_document(session, meta, parsed, sec.id, now)
        checks = cross_check(session, sec.id, sec.code, parsed["period_end"], parsed["values"], parsed["accounting_standard"],
                             edinet_doc_id=meta["docID"], edinet_submitted_at=meta["submitDateTime"])
        unavailable = record_unavailable(session, sec.code, meta["docID"], parsed["accounting_standard"], parsed["values"])
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
        compared, mismatched = compared_and_mismatch(checks)
        stats["documents_compared"] += compared
        stats["documents_mismatch"] += mismatched
        if unavailable:  # 取得の失敗ではない（読み取りは成功）。種類ごとに数える
            stats[f"liabilities_unavailable_{unavailable}"] += 1
        return "parsed"
    except edinet.EdinetAuthError:
        # 認証の失敗は書類の問題ではない：書類を「失敗」と記録せず、実行全体を止める（呼び出し元で処理）
        session.rollback()
        forget_pending(session)
        raise
    except Exception as e:  # 1件の書類の失敗で他を止めない。失敗も記録して原因を追えるようにする
        session.rollback()
        forget_pending(session)
        errors.append(f"{meta['docID']}（{stage}）: {redact(str(e))[:150]}")
        # ロールバックで、前回の「失敗」の記録（再試行の前に消したもの）が戻る。戻った場合は内容を更新する
        # （以前は同じ書類番号で新しい行を追加しようとして一意制約に反し、実行全体が止まっていた）
        failed = session.get(EdinetDocument, meta["docID"])
        if failed is None:
            session.add(EdinetDocument(
                doc_id=meta["docID"], security_id=sec.id, sec_code=meta["secCode"], edinet_code=meta.get("edinetCode"),
                filer_name=meta.get("filerName"), form_code=meta["formCode"], doc_description=meta.get("docDescription"),
                submitted_at=meta["submitDateTime"], period_end=None, accounting_standard=None, parse_status="failed",
                detail={"meta": keep, "error": redact(str(e))[:300], "error_type": stage}, fetched_at=now))
        else:
            failed.parse_status, failed.fetched_at = "failed", now
            failed.detail = {**(failed.detail or {}), "meta": keep, "error": redact(str(e))[:300], "error_type": stage,
                             "attempts": int((failed.detail or {}).get("attempts", 1)) + 1}
        session.commit()
        stats["documents_failed"] += 1
        stats[f"documents_failed_{stage}"] += 1
        return "failed"


def report(session: Session) -> dict:
    """保存済みの記録だけから、失敗・負債なし・不一致率を種類ごとに数える（読み取りのみ。何も書き込まない）。
    error_type・reason_type がない以前の記録は、種類を推定せず「unknown」（負債なしは会計基準から判定）として数える。"""
    from collections import Counter
    failed = Counter()
    for (detail,) in session.execute(select(EdinetDocument.detail).where(EdinetDocument.parse_status == "failed")):
        failed[(detail or {}).get("error_type", "unknown")] += 1
    unavailable = Counter()
    for (detail,) in session.execute(select(DataQualityCheck.detail).where(
            DataQualityCheck.dataset == QUALITY, DataQualityCheck.check_type == "liabilities_unavailable")):
        d = detail or {}
        unavailable[d.get("reason_type") or unavailable_type(d.get("standard"))] += 1
    parsed = session.scalar(select(func.count()).select_from(EdinetDocument).where(EdinetDocument.parse_status == "parsed"))
    compared, mismatched = mismatch_totals(session)
    runs = list(session.execute(select(IngestionRun.id, IngestionRun.status, IngestionRun.message)
                                .where(IngestionRun.dataset == DATASET).order_by(IngestionRun.id)))
    return {
        "documents_parsed": parsed,
        "documents_failed": sum(failed.values()),
        "documents_failed_by_type": {k: failed.get(k, 0) for k in ("api", "parse", "store", "unknown")},
        "failure_rate": (round(sum(failed.values()) / (parsed + sum(failed.values())), 4)
                         if parsed + sum(failed.values()) else None),
        "auth_error_runs": sum("'auth_error': True" in (m or "") for _, _, m in runs),
        "liabilities_unavailable_by_type": {k: unavailable.get(k, 0) for k in ("no_liabilities_element", "us_gaap")},
        "documents_compared": compared, "documents_mismatch": mismatched,
        "mismatch_rate_cumulative": round(mismatched / compared, 4) if compared else None,
        "mismatch_stopped_runs": [i for i, _, m in runs if (m or "").startswith("不一致率の停止基準")],
    }


def run(session: Session, codes: list[str] | None = None, start: date | None = None, end: date | None = None,
        recheck_only: bool = False, all_listed: bool = False, max_docs: int | None = None,
        mismatch_stop_rate: float = MISMATCH_STOP_RATE) -> IngestionRun:
    """start〜end に提出された決算書類を取得する。

    - codes（4桁の証券コード）を指定するか、all_listed=True で日本の普通株すべて（上場中）を対象にする。
    - max_docs：1回（1バッチ）あたりの書類の取得数の上限。all_listed のときの既定は DEFAULT_MAX_DOCS。
    - all_listed のときは、書類一覧を処理し終えた提出日を fetch_log（edinet_list）に記録し、次の実行では取り直さない。
      上限に達して途中で止まった提出日は記録しないので、次の実行でその日の残りから続ける（読み取り済みはスキップ）。
    - 前回失敗した書類は、書類一覧を取り直さずに、保存しておいたメタデータから先に再試行する。
    - recheck_only なら API に接続せず、保存済みの値から品質の記録を作り直す。
    - 不一致率（不一致のあった書類 ÷ 照合できた書類）をこの実行（バッチ）と全実行の累計で数え、照合できた書類が
      MISMATCH_MIN_DOCS 件を超えてどちらかが mismatch_stop_rate を超えたら、その場で止める（その日は「処理済み」にしない）。
      開始時点で累計がすでに超えていれば、API に接続せずに止める。"""
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
    if max_docs is not None and max_docs < 1:
        raise ValueError("max_docs は1以上を指定してください")
    scope = "日本の普通株すべて" if all_listed else f"{len(codes or [])}社"
    run = IngestionRun(dataset=DATASET, source=f"EDINET API v2（書類一覧・CSV、{scope}、上限 {max_docs or 'なし'} 件）",
                       status="running")
    session.add(run)
    session.commit()
    if not st.edinet_api_key.strip():
        run.status, run.message, run.finished_at = "skipped", "EDINET_API_KEY が未設定", datetime.now(timezone.utc)
        session.commit()
        return run
    base_compared, base_mismatch = mismatch_totals(session)
    if mismatch_exceeded(base_compared, base_mismatch, mismatch_stop_rate):
        run.status, run.finished_at = "failed", datetime.now(timezone.utc)
        run.message = (f"不一致率の停止基準を超えているため開始しない（累計 {base_mismatch}/{base_compared} 件、"
                       f"{base_mismatch / base_compared:.1%} ＞ {mismatch_stop_rate:.0%}）。原因を確認してから再開する")
        session.commit()
        return run
    q = select(Security).where(Security.market == "JP")
    q = q.where(Security.security_type == "common", Security.is_active.is_(True)) if all_listed else q.where(Security.code.in_(codes or []))
    targets = {_sec5(s.code): s for s in session.scalars(q)}
    client = edinet.EdinetClient(st.edinet_api_key.strip())
    stats = {"targets": len(targets), "list_requests": 0, "dates_skipped_listed": 0, "documents_found": 0,
             "documents_parsed": 0, "documents_failed": 0, "documents_retried": 0, "documents_skipped_existing": 0,
             "facts_added": 0, "facts_existing": 0, "checks": 0, "check_mismatch": 0, "limit_reached": False,
             # 失敗の種類（取得の失敗として数える）。認証エラー（401）は書類の失敗にせず、実行を止めて auth_error に記録する
             "documents_failed_api": 0, "documents_failed_parse": 0, "documents_failed_store": 0, "list_errors": 0,
             "auth_error": False,
             # 負債を取り出せなかった書類（取得の失敗ではない）
             "liabilities_unavailable_no_liabilities_element": 0, "liabilities_unavailable_us_gaap": 0,
             # 不一致率：照合できた書類と、不一致のあった書類（この実行の分）
             "documents_compared": 0, "documents_mismatch": 0, "mismatch_stop": None}
    errors: list[str] = []
    downloads = 0
    attempted: set[str] = set()  # この実行で処理を試みた書類（失敗した書類を同じ実行で2回試さない）

    def room() -> bool:
        return max_docs is None or downloads < max_docs

    def mismatch_stop() -> bool:
        """不一致率がバッチ単位・累計のどちらかで基準を超えたら止める。超えたときは理由を stats に残す。"""
        if stats["mismatch_stop"]:
            return True
        bc, bm = stats["documents_compared"], stats["documents_mismatch"]
        cc, cm = base_compared + bc, base_mismatch + bm
        for name, c, m in (("バッチ", bc, bm), ("累計", cc, cm)):
            if mismatch_exceeded(c, m, mismatch_stop_rate):
                stats["mismatch_stop"] = f"{name}の不一致率 {m}/{c} 件（{m / c:.1%}）が {mismatch_stop_rate:.0%} を超えた"
                return True
        return False

    try:
        try:
            # 1) 前回失敗した書類を、保存しておいたメタデータから再試行する（書類一覧は取り直さない）
            failed = list(session.scalars(select(EdinetDocument).where(EdinetDocument.parse_status == "failed",
                                                                        EdinetDocument.sec_code.in_(list(targets)))
                                          .order_by(EdinetDocument.submitted_at)))
            for doc in failed:
                if mismatch_stop():
                    break
                if not room():
                    stats["limit_reached"] = True
                    break
                meta = {**(doc.detail or {}).get("meta", {}), "docID": doc.doc_id, "secCode": doc.sec_code,
                        "formCode": doc.form_code, "submitDateTime": doc.submitted_at}
                _process_document(session, client, meta, targets[doc.sec_code], stats, errors)
                attempted.add(doc.doc_id)
                stats["documents_retried"] += 1
                downloads += 1
            # 2) 提出日ごとに書類一覧を取得し、対象の決算書類を読み取る
            listed = {k for (k,) in session.execute(select(FetchLog.key).where(FetchLog.dataset == LIST_DATASET))} if all_listed else set()
            d = start
            while d <= end and not stats["limit_reached"] and not mismatch_stop():
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
                        stats["auth_error"] = True
                        break
                    except Exception as e:  # この日は記録しないので、次の実行で取り直す
                        stats["list_errors"] += 1
                        errors.append(f"{d}（書類一覧）: {redact(str(e))[:150]}")
                        d += timedelta(days=1)
                        continue
                    complete = True
                    for meta in docs:
                        stats["documents_found"] += 1
                        prev = session.get(EdinetDocument, meta["docID"])
                        if (prev and prev.parse_status == "parsed") or meta["docID"] in attempted:
                            stats["documents_skipped_existing"] += 1  # 読み取り済み、またはこの実行で再試行済み（失敗は記録済み）
                            continue
                        if mismatch_stop():  # 不一致率の停止：この日の残りは次の実行で（この日は「処理済み」にしない）
                            complete = False
                            break
                        if not room():  # 上限：この日の残りは次の実行で（この日は「処理済み」にしない）
                            stats["limit_reached"] = True
                            complete = False
                            break
                        _process_document(session, client, meta, targets[meta["secCode"]], stats, errors)
                        attempted.add(meta["docID"])
                        downloads += 1  # 失敗した書類も記録済みで、次の実行の 1) で再試行される
                    if complete and mismatch_stop():  # この日の最後の書類で基準を超えた：この日も「処理済み」にしない
                        complete = False
                    if complete and all_listed:
                        log = session.scalar(select(FetchLog).where(FetchLog.dataset == LIST_DATASET, FetchLog.key == d.isoformat()))
                        if log is None:
                            session.add(FetchLog(dataset=LIST_DATASET, key=d.isoformat(), status="success", rows=len(docs),
                                                 fetched_at=datetime.now(timezone.utc)))
                        session.commit()
                d += timedelta(days=1)
        except edinet.EdinetAuthError as e:  # 書類の取得中の認証の失敗：日付も書類も「処理済み」「失敗」にしない
            errors.append(str(e))
            stats["auth_error"] = True
    finally:
        client.close()
    run = session.get(IngestionRun, run.id)
    run.rows, run.finished_at = stats["facts_added"], datetime.now(timezone.utc)
    run.status = "success" if not errors else ("partial" if stats["documents_parsed"] else "failed")
    head = []
    if stats["mismatch_stop"]:  # 自動停止は「失敗」として示し、原因の確認を求める
        run.status = "failed"
        head.append(f"不一致率の停止基準により自動停止：{stats['mismatch_stop']}。原因を確認してから再開する")
    bc, bm = stats["documents_compared"], stats["documents_mismatch"]
    cc, cm = base_compared + bc, base_mismatch + bm
    stats["mismatch_rate_batch"] = f"{bm}/{bc}" + (f"（{bm / bc:.1%}）" if bc else "")
    stats["mismatch_rate_cumulative"] = f"{cm}/{cc}" + (f"（{cm / cc:.1%}）" if cc else "")
    # 要約（失敗の種類・不一致率）を先頭に置く（メッセージは1,000文字で切るため）
    head.append(f"取得の失敗 {stats['documents_failed']}（API通信 {stats['documents_failed_api']}・"
                f"書類の読み取り {stats['documents_failed_parse']}・保存 {stats['documents_failed_store']}）"
                f"・書類一覧の失敗 {stats['list_errors']}・認証エラー {'あり' if stats['auth_error'] else 'なし'}"
                f"・負債なし（失敗ではない）{stats['liabilities_unavailable_no_liabilities_element']}"
                f"・米国基準で負債取得不可（失敗ではない）{stats['liabilities_unavailable_us_gaap']}"
                f"・不一致率 バッチ {stats['mismatch_rate_batch']} 累計 {stats['mismatch_rate_cumulative']}")
    run.message = ("; ".join(head + [str(stats)] + errors[:5]))[:1000]
    session.commit()
    return run
