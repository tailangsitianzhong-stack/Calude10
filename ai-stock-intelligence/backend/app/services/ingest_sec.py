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


# 公開流通時価総額（EntityPublicFloat）の桁の誤りを見分ける基準。
# 企業が「千ドル単位の値をドルとして」提出する誤りがあり（例 Cabot 4,429兆ドル、TTM 4.2兆ドル）、
# そのまま並べると小型株が上位に入る。同じ時期に同じ企業が提出した株数・総資産と比べて、ありえない値を除く。
# 実データ（2026-11、流通時価総額300億ドル以上の340社）での確認：
#   正常な値：総資産の最大22倍（PLTR）。1株あたりが高い企業：NVR 約7,600ドル（総資産の3.7倍）、
#            BKNG 分割前 約5,900ドル（6.4倍）、AZO 約3,900ドル（2.9倍）
#   誤りの値（23社）：すべて総資産の50倍超（最小 TRMK 50.1倍。TRMK は1株あたり約1.7万ドルでも該当）
MAX_FLOAT_TO_ASSETS = 50.0  # 流通時価総額 ÷ 総資産（ドル建ての総資産がある場合）
MAX_IMPLIED_PRICE = 5_000.0  # 流通時価総額 ÷ 発行済株式数（1株あたりの株価に相当、ドル）
PRICE_CHECK_MIN_FLOAT_TO_ASSETS = 20.0  # 1株あたりが高くても、総資産の20倍以下なら正常とみなす（NVR・BKNG などの高株価企業を除外しない）


def _frames_by_cik(client: sec.SecClient, taxonomy: str, tag: str, unit: str,
                   periods: list[str]) -> tuple[dict[int, tuple[float, str]], dict[str, float]]:
    """frames を最新の期間から順に見て、各社の最新値（値・提出書類番号）と、提出書類番号ごとの値を返す。"""
    latest: dict[int, tuple[float, str]] = {}
    by_accn: dict[str, float] = {}
    for p in periods:
        fr = client.frame(taxonomy, tag, unit, p)
        for x in (fr or {}).get("data", []):
            latest.setdefault(int(x["cik"]), (float(x["val"]), x.get("accn")))
            by_accn.setdefault(x.get("accn"), float(x["val"]))
    return latest, by_accn


def float_anomaly(public_float: float, shares: float | None, assets: float | None) -> dict | None:
    """流通時価総額が、同じ企業の株数・総資産と比べてありえない値なら、その理由を返す（正常なら None）。"""
    implied = public_float / shares if shares else None
    ratio = public_float / assets if assets else None
    reasons = []
    if ratio is not None and ratio > MAX_FLOAT_TO_ASSETS:
        reasons.append(f"流通時価総額が総資産の{ratio:,.0f}倍（上限{MAX_FLOAT_TO_ASSETS:,.0f}倍）")
    if implied is not None and implied > MAX_IMPLIED_PRICE and (ratio is None or ratio > PRICE_CHECK_MIN_FLOAT_TO_ASSETS):
        reasons.append(f"流通時価総額÷発行済株式数＝1株あたり{implied:,.0f}ドル（上限{MAX_IMPLIED_PRICE:,.0f}ドル）")
    if not reasons:
        return None
    return {"public_float_usd": public_float, "shares_outstanding": shares, "assets_usd": assets,
            "implied_price_usd": implied, "float_to_assets": ratio,
            "reason": "企業が提出した公開流通時価総額の桁が誤っている可能性が高いため、規模の順位づけから除外：" + "、".join(reasons)}


def pick_universe(session: Session, client: sec.SecClient, size: int, extra: list[str]) -> tuple[list[Security], dict]:
    """公開流通時価総額の大きい順に size 社＋extra を選ぶ。戻り値の2つ目は、桁の誤りとして除外した企業（CIK → 詳細）。"""
    periods = float_periods()
    floats, _ = _frames_by_cik(client, "dei", "EntityPublicFloat", "USD", periods)
    secs = list(session.scalars(select(Security).where(Security.market == "US", Security.is_active.is_(True),
                                                        Security.security_type == "common", Security.cik.is_not(None))))
    by_cik: dict[int, Security] = {}
    for s in secs:  # 同じ企業の複数クラス（例 BRK.A / BRK.B）は1社として扱う
        by_cik.setdefault(s.cik, s)
    # 株数・総資産は、流通時価総額と同じ提出書類（10-Kの表紙と本文）の値を優先して照合する。
    # 時期の違う値を比べると、株式分割（例 BKNG の2026年の25対1）の前後で1株あたりの値がずれるため
    shares, shares_accn = _frames_by_cik(client, "dei", "EntityCommonStockSharesOutstanding", "shares", periods)
    assets, assets_accn = _frames_by_cik(client, "us-gaap", "Assets", "USD", periods)

    def same_filing(latest: dict, by_accn: dict, c: int, accn: str) -> float | None:
        return by_accn.get(accn) if accn in by_accn else (latest[c][0] if c in latest else None)

    ranked, rejected = [], {}
    for c in sorted((c for c in by_cik if c in floats), key=lambda c: -floats[c][0]):
        val, accn = floats[c]
        bad = float_anomaly(val, same_filing(shares, shares_accn, c, accn), same_filing(assets, assets_accn, c, accn))
        if bad:
            rejected[c] = bad
            continue
        ranked.append(c)
        if len(ranked) >= size:
            break
    chosen = {c: by_cik[c] for c in ranked}
    for t in extra:
        s = next((x for x in secs if x.code == t), None)
        if s and s.cik not in chosen:
            chosen[s.cik] = s
    return list(chosen.values()), {c: (by_cik[c], d) for c, d in rejected.items()}


GAP_FILL_DAYS = 550  # companyfacts に未反映の提出書類を、直接読んで補う対象の期間（提出日が直近この日数以内）
STALE_DAYS = 120


def missing_filings(companyfacts: dict, submissions: dict | None, today: date | None = None) -> list[dict]:
    """SEC の提出書類一覧にあるのに、companyfacts API にまだ含まれていない決算書類（新しい順）。

    実データの例（2026-11-04）：ABT・V は 2026-07 提出の10-Qが、NEE は 2026-07・10 提出の10-Qが companyfacts になかった。
    """
    if not submissions:
        return []
    have = {x["accn"] for g in companyfacts.get("facts", {}).values() for v in g.values()
            for xs in v.get("units", {}).values() for x in xs}
    cutoff = (today or date.today()).toordinal() - GAP_FILL_DAYS
    return [f for f in sec.periodic_filings(submissions)
            if f["accn"] not in have and date.fromisoformat(f["filed"]).toordinal() >= cutoff]


def ingest_company(session: Session, client: sec.SecClient, s: Security) -> int:
    cf = client.companyfacts(s.cik)
    if cf is None:
        session.add(DataQualityCheck(dataset="sec", subject=s.code, check_type="missing", severity="warn",
                                     detail={"reason": "SECにXBRLの財務データがない"}))
        return 0
    facts = sec.extract_facts(cf)
    # companyfacts に未反映の提出書類は、提出書類のXBRLを直接読んで補う（SEC側の反映遅れへの対策）
    subs = client.submissions(s.cik)
    filled, unreadable = [], []
    for f in missing_filings(cf, subs):
        try:
            xml = client.filing_instance(s.cik, f["accn"])
        except Exception as e:  # 1件の書類の失敗で、他の書類・企業を止めない
            unreadable.append({**f, "error": redact(str(e))[:120]})
            continue
        if xml is None:
            unreadable.append({**f, "error": "XBRLインスタンスが見つからない"})
            continue
        got = sec.extract_facts(sec.parse_instance(xml, f["accn"], f["form"], f["filed"]))
        facts += got
        filled.append({**f, "facts": len(got)})
    now = datetime.now(timezone.utc)
    # 保存済みの事実は変わらないので、新しい提出書類の分だけ追加する
    have = {(t, u, ps, pe, a) for t, u, ps, pe, a in session.execute(select(
        FinancialFact.tag, FinancialFact.unit, FinancialFact.period_start, FinancialFact.period_end, FinancialFact.accn)
        .where(FinancialFact.security_id == s.id))}
    added = 0
    for f in facts:
        ps = date.fromisoformat(f["period_start"]) if f["period_start"] else None
        pe = date.fromisoformat(f["period_end"])
        key = (f["tag"], f["unit"], ps, pe, f["accn"])
        if key in have:
            continue
        have.add(key)
        session.add(FinancialFact(security_id=s.id, concept=f["concept"], tag=f["tag"], unit=f["unit"], period_start=ps,
                                  period_end=pe, fiscal_year=f["fiscal_year"], fiscal_period=f["fiscal_period"],
                                  form=f["form"], accn=f["accn"], filed=date.fromisoformat(f["filed"]), value=f["value"],
                                  source="SEC EDGAR XBRL", fetched_at=now))
        added += 1
    if filled:
        session.add(DataQualityCheck(dataset="sec", subject=s.code, check_type="gap_filled", severity="info",
                                     detail={"note": "SECの companyfacts API に未反映の提出書類を、書類のXBRLから直接取得した",
                                             "filings": filled}))
    # 古さの判定：保存済みの最新の提出日と、SECの提出書類一覧の最新の決算書類を比べ、原因を区別して記録する
    latest_filed = session.scalar(select(func.max(FinancialFact.filed)).where(FinancialFact.security_id == s.id))
    latest_filed = max(filter(None, [latest_filed, *(date.fromisoformat(f["filed"]) for f in facts)]), default=None)
    periodic = sec.periodic_filings(subs) if subs else []
    sec_latest = date.fromisoformat(periodic[0]["filed"]) if periodic else None
    if unreadable:
        session.add(DataQualityCheck(dataset="sec", subject=s.code, check_type="missing", severity="warn",
                                     detail={"reason": "SECに提出済みの決算書類のXBRLを読めなかった", "filings": unreadable}))
    if latest_filed and (date.today() - latest_filed).days > STALE_DAYS:
        detail = {"latest_filed": str(latest_filed), "sec_latest_periodic_filing": str(sec_latest) if sec_latest else None}
        if sec_latest and sec_latest > latest_filed:
            detail["note"] = "SECにはより新しい決算書類があるが、財務データを取得できていない"
        else:
            detail["note"] = f"最新の決算書類の提出から{STALE_DAYS}日超（SECに新しい決算書類がない）"
        session.add(DataQualityCheck(dataset="sec", subject=s.code, check_type="stale", severity="warn", detail=detail))
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
        session.execute(delete(DataQualityCheck).where(DataQualityCheck.dataset == "sec"))
        universe, rejected = pick_universe(session, client, limit or st.sec_universe_size, extra)
        for sec_obj, d in rejected.values():
            session.add(DataQualityCheck(dataset="sec", subject=sec_obj.code, check_type="float_anomaly", severity="warn",
                                         detail=d))
        session.commit()
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
