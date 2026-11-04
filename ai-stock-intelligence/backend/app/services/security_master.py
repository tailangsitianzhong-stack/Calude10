"""銘柄マスタの取り込み。

- 日本株：JPX「東証上場銘柄一覧」（Excel、毎月更新）
  https://www.jpx.co.jp/markets/statistics-equities/misc/01.html
- 米国株：Nasdaq Trader の Symbol Directory（nasdaqlisted.txt / otherlisted.txt、毎日更新）
  https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs

どちらも公開されている一覧で、株価や財務の数値は含まない。
"""
from datetime import date, datetime, timezone
from pathlib import Path

import openpyxl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import IngestionRun, Security
from .text import normalize

JPX_SOURCE = "JPX 東証上場銘柄一覧"
NASDAQ_SOURCE = "Nasdaq Trader Symbol Directory"
# otherlisted.txt の取引所コード
US_EXCHANGES = {"A": "NYSE American", "N": "NYSE", "P": "NYSE Arca", "Z": "Cboe BZX", "V": "IEX"}
NASDAQ_TIERS = {"Q": "Nasdaq Global Select", "G": "Nasdaq Global Market", "S": "Nasdaq Capital Market"}


def _jp_type(segment: str) -> str:
    if "ETF" in segment or "ETN" in segment:
        return "etf"
    if "REIT" in segment or "ベンチャーファンド" in segment or "インフラファンド" in segment:
        return "reit"
    if "内国株式" in segment or "外国株式" in segment:
        return "common"
    return "other"


def parse_jpx(path: Path) -> tuple[list[dict], date | None]:
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb.active
    rows, as_of = [], None
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        d, code, name, segment, _c33, sec33, _c17, _sec17, _sc, size = r[:10]
        if code is None or name is None:
            continue
        as_of = as_of or datetime.strptime(str(d), "%Y%m%d").date()
        rows.append({
            "market": "JP", "code": str(code).strip(), "name": str(name).strip(),
            "exchange": str(segment), "sector": None if sec33 in (None, "-") else str(sec33),
            "size_class": None if size in (None, "-") else str(size), "security_type": _jp_type(str(segment)),
        })
    return rows, as_of


_US_OTHER = (" warrant", " right", " unit", "preferred", " notes", "debenture", "% ", " depositary share")


def _us_type(name: str, etf: str) -> str:
    if etf == "Y":
        return "etf"
    n = " " + name.lower()
    # 普通株式のADR（"American Depositary Shares"）は common に残し、優先株の預託証券などは other にする
    if "american depositary" in n and "preferred" not in n:
        return "common"
    return "other" if any(k in n for k in _US_OTHER) else "common"


def parse_nasdaq(nasdaq_listed: Path, other_listed: Path) -> tuple[list[dict], date | None]:
    rows, as_of = [], None

    def read(path: Path):
        nonlocal as_of
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        header = lines[0].split("|")
        for line in lines[1:]:
            if line.startswith("File Creation Time"):
                stamp = line.split(":", 1)[1].split("|")[0].strip()  # MMDDYYYYHH:MM
                as_of = datetime.strptime(stamp[:8], "%m%d%Y").date()
                continue
            yield dict(zip(header, line.split("|")))

    for r in read(nasdaq_listed):
        if r.get("Test Issue") == "Y":
            continue
        rows.append({"market": "US", "code": r["Symbol"], "name": r["Security Name"],
                     "exchange": NASDAQ_TIERS.get(r.get("Market Category", ""), "Nasdaq"),
                     "security_type": _us_type(r["Security Name"], r.get("ETF", "")), "sector": None, "size_class": None})
    for r in read(other_listed):
        if r.get("Test Issue") == "Y":
            continue
        rows.append({"market": "US", "code": r["ACT Symbol"], "name": r["Security Name"],
                     "exchange": US_EXCHANGES.get(r.get("Exchange", ""), r.get("Exchange", "")),
                     "security_type": _us_type(r["Security Name"], r.get("ETF", "")), "sector": None, "size_class": None})
    return rows, as_of


def upsert(session: Session, rows: list[dict], source: str, as_of: date | None, dataset: str) -> IngestionRun:
    """銘柄を追加・更新する。一覧から消えた銘柄は削除せず is_active=False にする。"""
    now = datetime.now(timezone.utc)
    run = IngestionRun(dataset=dataset, source=source, started_at=now, status="running", source_as_of=as_of)
    session.add(run)
    market = rows[0]["market"] if rows else None
    existing = {(s.market, s.code): s for s in session.scalars(select(Security).where(Security.market == market))}
    seen = set()
    for r in rows:
        key = (r["market"], r["code"])
        if key in seen:
            continue
        seen.add(key)
        s = existing.get(key)
        if s is None:
            s = Security(market=r["market"], code=r["code"])
            session.add(s)
        s.name, s.name_search = r["name"], normalize(r["name"] + " " + r["code"])
        s.exchange, s.sector, s.size_class = r["exchange"], r["sector"], r["size_class"]
        s.security_type, s.is_active = r["security_type"], True
        s.source, s.source_as_of, s.fetched_at = source, as_of, now
    for key, s in existing.items():
        if key not in seen:
            s.is_active = False
    run.rows, run.status, run.finished_at = len(seen), "success", datetime.now(timezone.utc)
    session.commit()
    return run
