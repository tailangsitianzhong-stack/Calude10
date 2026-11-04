"""J-Quants の財務情報（/v2/fins/summary）を時点つきで保存する（APIキー設定時のみ）。

- 開示1件を jp_disclosures に（元のデータごと）、実績の値を financial_facts に保存する。どちらも追記のみ。
  既存の行は更新・削除しない（訂正開示は新しい開示番号の別の行として残る）。
- 日本株（market='JP'）の銘柄にだけ書き込む。米国株の行には触れない。
- 取得の方法：
    codes   … 指定した銘柄の全期間（検証用。1銘柄1リクエスト）
    dates   … 指定した開示日の全銘柄（1日1リクエスト＋ページ送り）
    window  … Freeプランで取得できる期間（12週間前〜2年12週間前）の開示日のうち、まだ取得していない日（max_dates 日まで）
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import DataQualityCheck, FetchLog, FinancialFact, IngestionRun, JpDisclosure, Security
from ..providers import jquants
from ..providers.http import redact

DATASET = "financials_jp"
QUALITY = "jquants_fins"
SOURCE = "J-Quants fins/summary"
WINDOW_DAYS = 730  # Freeプラン：遅延（12週間）からさらに2年


def jp_security_map(session: Session) -> dict[str, int]:
    return {c: i for i, c in session.execute(select(Security.id, Security.code).where(Security.market == "JP"))}


def store_records(session: Session, rows: list[dict], secmap: dict[str, int], now: datetime) -> dict:
    """応答のレコードを保存する。戻り値は件数の内訳。既存の開示・値は追加しない（重複登録の防止）。"""
    stats = {"records": len(rows), "disclosures_added": 0, "disclosures_existing": 0, "facts_added": 0,
             "facts_existing": 0, "unmatched": [], "unknown_doc": [], "problems": []}
    have_disc = {d for (d,) in session.execute(select(JpDisclosure.disc_no).where(
        JpDisclosure.disc_no.in_([r.get("DiscNo") for r in rows])))} if rows else set()
    for r in rows:
        p = jquants.parse_fin_record(r)
        d = p["disclosure"]
        sid = secmap.get(d["code"]) if d["code_matchable"] else None
        if d["kind"] == "unknown":
            stats["unknown_doc"].append({"disc_no": d["disc_no"], "doc_type": d["doc_type"]})
        if sid is None:
            stats["unmatched"].append({"code": d["jq_code"], "disc_no": d["disc_no"], "doc_type": d["doc_type"]})
        for pr in p["problems"]:
            stats["problems"].append({"code": d["jq_code"], "disc_no": d["disc_no"], **pr})
        if d["disc_no"] in have_disc:
            stats["disclosures_existing"] += 1
        else:
            have_disc.add(d["disc_no"])
            session.add(JpDisclosure(
                disc_no=d["disc_no"], security_id=sid, jq_code=d["jq_code"], disc_date=d["disc_date"],
                disc_time=d["disc_time"], doc_type=d["doc_type"][:64], kind=d["kind"],
                accounting_standard=d["accounting_standard"], consolidated=d["consolidated"],
                period_type=d["period_type"], period_start=d["period_start"], period_end=d["period_end"],
                fy_start=d["fy_start"], fy_end=d["fy_end"], raw=d["raw"], source=SOURCE, fetched_at=now))
            stats["disclosures_added"] += 1
        if sid is None or not p["facts"]:
            continue
        have = {(t, u, ps, pe) for t, u, ps, pe in session.execute(
            select(FinancialFact.tag, FinancialFact.unit, FinancialFact.period_start, FinancialFact.period_end)
            .where(FinancialFact.security_id == sid, FinancialFact.accn == d["disc_no"]))}
        for f in p["facts"]:
            key = (f["tag"], f["unit"], f["period_start"], f["period_end"])
            if key in have:
                stats["facts_existing"] += 1
                continue
            have.add(key)
            session.add(FinancialFact(security_id=sid, concept=f["concept"], tag=f["tag"], unit=f["unit"],
                                      period_start=f["period_start"], period_end=f["period_end"],
                                      fiscal_year=f["fiscal_year"], fiscal_period=f["fiscal_period"], form=f["form"],
                                      accn=f["accn"], filed=f["filed"], value=f["value"], source=SOURCE, fetched_at=now))
            stats["facts_added"] += 1
    return stats


def window_dates(today: date, plan: str, fetched: set[str], max_dates: int) -> list[date]:
    """取得できる期間の平日のうち、まだ取得していない開示日（新しい順）。"""
    newest = today - timedelta(days=jquants.DELAY_DAYS.get(plan, 0))
    out, d = [], newest
    while d >= newest - timedelta(days=WINDOW_DAYS) and len(out) < max_dates:
        if d.weekday() < 5 and d.isoformat() not in fetched:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _record_quality(session: Session, stats: dict) -> None:
    for u in stats["unmatched"]:
        session.add(DataQualityCheck(dataset=QUALITY, subject=u["code"], check_type="unmatched", severity="info",
                                     detail={"reason": "銘柄一覧に対応するコードがない（上場廃止・優先株など）。開示の元データだけ保存", **u}))
    for u in stats["unknown_doc"]:
        session.add(DataQualityCheck(dataset=QUALITY, subject=u["disc_no"], check_type="unknown_doc_type", severity="warn",
                                     detail={"reason": "公式の開示書類種別の一覧にない種別。値は保存しない", **u}))
    for pr in stats["problems"]:
        session.add(DataQualityCheck(dataset=QUALITY, subject=pr["code"], check_type="invalid_value", severity="warn",
                                     detail=pr))


def run(session: Session, codes: list[str] | None = None, dates: list[date] | None = None,
        max_dates: int | None = None) -> IngestionRun:
    st = get_settings()
    # 前回の実行が途中で止まった（プロセスの強制終了など）場合、その記録を「中断」にする。
    # 取得済みの開示日は fetch_log に残っているので、今回の実行は残りの日付から再開する
    for old in session.scalars(select(IngestionRun).where(IngestionRun.dataset == DATASET, IngestionRun.status == "running")):
        old.status, old.message = "interrupted", "前回の実行が途中で終了した（取得済みの開示日は保存済み。残りは次の実行で取得）"
        old.finished_at = old.finished_at or datetime.now(timezone.utc)
    session.commit()
    mode = "codes" if codes else ("dates" if dates else "window")
    run = IngestionRun(dataset=DATASET, source=f"J-Quants API V2 fins/summary（{st.jquants_plan}・{mode}）", status="running")
    session.add(run)
    session.commit()
    if not st.has_credential("jquants"):
        run.status, run.message, run.finished_at = "skipped", "APIキー未設定（JQUANTS_API_KEY またはネットワークシークレット）", datetime.now(timezone.utc)
        session.commit()
        return run
    client = jquants.JQuantsClient(st.jquants_api_key, st.jquants_plan, network_secret=st.via_network_secret("jquants"))
    secmap = jp_security_map(session)
    totals = {"requests_ok": 0, "records": 0, "disclosures_added": 0, "facts_added": 0, "facts_existing": 0,
              "unmatched": 0, "problems": 0}
    errors: list[str] = []
    stop = False

    def one(label: str, fetch, log_key: str | None):
        nonlocal stop
        try:
            rows = fetch()
        except (PermissionError, jquants.PlanNotAvailable) as e:  # 認証・プランの問題は続けても同じ結果になる
            errors.append(f"{label}: {redact(str(e))[:150]}")
            stop = True
            return
        except Exception as e:  # 1件の失敗で他を止めない
            session.rollback()
            errors.append(f"{label}: {redact(str(e))[:150]}")
            return
        try:  # 保存と取得済みの記録は1つのトランザクション（途中で止まっても、その日の分は全部か何もなし）
            stats = store_records(session, rows, secmap, datetime.now(timezone.utc))
            _record_quality(session, stats)
            if log_key:
                # 取得済みの日付をもう一度取得した場合（--dates の指定など）は、記録を更新する（同じキーを2行にしない）
                log = session.scalar(select(FetchLog).where(FetchLog.dataset == DATASET, FetchLog.key == log_key))
                if log is None:
                    session.add(FetchLog(dataset=DATASET, key=log_key, status="success", rows=len(rows),
                                         fetched_at=datetime.now(timezone.utc)))
                else:
                    log.status, log.rows, log.fetched_at = "success", len(rows), datetime.now(timezone.utc)
            session.commit()
        except Exception as e:  # 保存の失敗も、その日付だけの失敗として記録し、他の日付は続ける
            session.rollback()
            errors.append(f"{label}: 保存に失敗 {redact(str(e))[:150]}")
            return
        totals["requests_ok"] += 1
        for k in ("records", "disclosures_added", "facts_added", "facts_existing"):
            totals[k] += stats[k]
        totals["unmatched"] += len(stats["unmatched"])
        totals["problems"] += len(stats["problems"])

    try:
        if codes:
            for c in codes:
                if stop:
                    break
                one(c, lambda c=c: client.fins_summary(code=c), None)
        else:
            if not dates:
                fetched = {k for (k,) in session.execute(select(FetchLog.key).where(FetchLog.dataset == DATASET))}
                dates = window_dates(date.today(), st.jquants_plan, fetched, max_dates or 20)
            for d in dates:
                if stop:
                    break
                one(d.isoformat(), lambda d=d: client.fins_summary(disc_date=d), d.isoformat())
    finally:
        client.close()
    run = session.get(IngestionRun, run.id)
    run.rows, run.finished_at = totals["facts_added"], datetime.now(timezone.utc)
    run.status = "success" if not errors else ("partial" if totals["requests_ok"] else "failed")
    summary = (f"取得 {totals['records']}件の開示（新規 {totals['disclosures_added']}）、値の保存 {totals['facts_added']}件"
               f"（既存 {totals['facts_existing']}件は追加せず）、銘柄一覧にないコード {totals['unmatched']}件、"
               f"変換できない値 {totals['problems']}件")
    run.message = ("; ".join([summary] + errors[:5]))[:1000]
    run.source_as_of = session.scalar(select(func.max(JpDisclosure.disc_date)))
    session.commit()
    return run
