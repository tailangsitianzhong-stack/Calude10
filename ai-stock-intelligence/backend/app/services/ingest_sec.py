"""SECの財務データ（XBRL companyfacts）を時点つきで保存する。

対象：公開流通時価総額（10-Kの表紙に記載、SECの frames API）の大きい順に SEC_UNIVERSE_SIZE 社 ＋ SEC_EXTRA_TICKERS。
株価データがない段階でも、規模の大きい企業から順に財務データを用意するための選び方（暫定）。
"""
from datetime import date, datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..providers.http import redact
from ..config import get_settings
from ..models import DataQualityCheck, FinancialFact, IngestionRun, Security
from ..providers import sec

def float_periods(today: date | None = None, quarters: int = 8) -> list[str]:
    """公開流通時価総額は各社の第2四半期末時点の値で、決算期によって暦の四半期がずれる（例：Appleは3月末）。
    そのため直近8四半期の frames を新しい順に見て、各社の最新値を使う。"""
    today = today or date.today()
    y, q = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(quarters):
        out.append(f"CY{y}Q{q}I")
        y, q = (y - 1, 4) if q == 1 else (y, q - 1)
    return out


def normalize_ticker(t: str) -> str:
    return t.upper().replace("-", ".")


def attach_ciks(session: Session, client: sec.SecClient) -> int:
    data = client.tickers()
    by_ticker = {normalize_ticker(v["ticker"]): int(v["cik_str"]) for v in data.values()}
    n = 0
    for s in session.scalars(select(Security).where(Security.market == "US")):
        cik = by_ticker.get(normalize_ticker(s.code))
        if cik and s.cik != cik:
            s.cik = cik
            n += 1
    session.commit()
    return n


def pick_universe(session: Session, client: sec.SecClient, size: int, extra: list[str]) -> list[Security]:
    floats: dict[int, float] = {}
    for p in float_periods():  # 最新の期間から順に、値のある企業の公開流通時価総額を使う
        fr = client.frame("dei", "EntityPublicFloat", "USD", p)
        for x in (fr or {}).get("data", []):
            floats.setdefault(int(x["cik"]), float(x["val"]))
    secs = list(session.scalars(select(Security).where(Security.market == "US", Security.is_active.is_(True),
                                                        Security.security_type == "common", Security.cik.is_not(None))))
    by_cik: dict[int, Security] = {}
    for s in secs:  # 同じ企業の複数クラス（例 BRK.A / BRK.B）は1社として扱う
        by_cik.setdefault(s.cik, s)
    ranked = sorted((c for c in by_cik if c in floats), key=lambda c: -floats[c])[:size]
    chosen = {c: by_cik[c] for c in ranked}
    for t in extra:
        s = next((x for x in secs if x.code == t), None)
        if s and s.cik not in chosen:
            chosen[s.cik] = s
    return list(chosen.values())


def ingest_company(session: Session, client: sec.SecClient, s: Security) -> int:
    cf = client.companyfacts(s.cik)
    if cf is None:
        session.add(DataQualityCheck(dataset="sec", subject=s.code, check_type="missing", severity="warn",
                                     detail={"reason": "SECにXBRLの財務データがない"}))
        return 0
    facts = sec.extract_facts(cf)
    now = datetime.now(timezone.utc)
    # 保存済みの事実は変わらないので、新しい提出書類の分だけ追加する
    have = {(t, u, ps, pe, a) for t, u, ps, pe, a in session.execute(select(
        FinancialFact.tag, FinancialFact.unit, FinancialFact.period_start, FinancialFact.period_end, FinancialFact.accn)
        .where(FinancialFact.security_id == s.id))}
    added = 0
    for f in facts:
        ps = date.fromisoformat(f["period_start"]) if f["period_start"] else None
        pe = date.fromisoformat(f["period_end"])
        if (f["tag"], f["unit"], ps, pe, f["accn"]) in have:
            continue
        session.add(FinancialFact(security_id=s.id, concept=f["concept"], tag=f["tag"], unit=f["unit"], period_start=ps,
                                  period_end=pe, fiscal_year=f["fiscal_year"], fiscal_period=f["fiscal_period"],
                                  form=f["form"], accn=f["accn"], filed=date.fromisoformat(f["filed"]), value=f["value"],
                                  source="SEC EDGAR XBRL", fetched_at=now))
        added += 1
    latest_filed = max((date.fromisoformat(f["filed"]) for f in facts), default=None)
    if latest_filed and (date.today() - latest_filed).days > 120:
        session.add(DataQualityCheck(dataset="sec", subject=s.code, check_type="stale", severity="warn",
                                     detail={"latest_filed": str(latest_filed), "note": "最新の提出から120日超"}))
    return added


SEC_UA_MISSING = "SEC_USER_AGENT が未設定（アプリ名と連絡先メールアドレスが必要）のため、SEC から取得していません"


def run(session: Session, limit: int | None = None) -> IngestionRun:
    st = get_settings()
    if not st.sec_ua_configured:
        # 仮の連絡先では SEC に接続しない（SEC の規則）。取得しなかったことを記録だけする
        run = IngestionRun(dataset="sec_financials", source="SEC EDGAR XBRL companyfacts", status="failed", rows=0,
                           message=SEC_UA_MISSING, finished_at=datetime.now(timezone.utc))
        session.add(run)
        session.commit()
        return run
    run = IngestionRun(dataset="sec_financials", source="SEC EDGAR XBRL companyfacts", status="running")
    session.add(run)
    session.commit()
    client = sec.SecClient(st.sec_ua)
    added, errors, done = 0, [], 0
    try:
        attach_ciks(session, client)
        extra = [t.strip().upper() for t in st.sec_extra_tickers.split(",") if t.strip()]
        universe = pick_universe(session, client, limit or st.sec_universe_size, extra)
        session.execute(delete(DataQualityCheck).where(DataQualityCheck.dataset == "sec"))
        for s in universe:
            try:
                added += ingest_company(session, client, s)
                session.commit()
                done += 1
            except Exception as e:
                session.rollback()
                errors.append(f"{s.code}: {redact(str(e))[:100]}")
    except Exception as e:
        errors.append(redact(str(e))[:300])
    finally:
        client.close()
    run = session.get(IngestionRun, run.id)
    run.rows, run.finished_at = added, datetime.now(timezone.utc)
    run.status = "success" if not errors else ("partial" if done else "failed")
    run.message = ("; ".join(errors[:5]))[:1000] or None
    run.source_as_of = session.scalar(select(func.max(FinancialFact.filed)))
    session.commit()
    return run
