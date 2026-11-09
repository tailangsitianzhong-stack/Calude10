"""株価の接続状態の判定（銘柄ページ・株価の推移・データの状況・検索結果で共通に使う）。

「APIキーが設定されているか」と「株価を取得できているか」を分けて判定する。状態は次の7つ（上から順に評価し、
最初に当てはまったものにする。複数の条件が同時に成り立つときの優先順位でもある）：

1. not_configured          APIキー未設定（日本株：J-Quants の認証情報がない。米国株：取得に使える取得元が1つもない
                           ＝ingest_us_prices.provider_chain() が空）。過去の失敗の記録や保存済みの株価があっても、
                           更新されない状態なのでこれを優先する（保存済みの株価は基準日つきで示し、値は作らない）。
2. provider_error          取得失敗・API障害（設定済みで、最後に終わった取得の実行が failed）。
3. configured_not_fetched  APIキー設定済み・未取得（その銘柄・その市場の株価がまだない）。
4. stale                   保存データあり・更新遅延（最新の基準日が、提供元の遅延＋許容日数より古い）。
5. partial                 一部銘柄のみ取得成功（市場全体の判定だけ：取得対象のうち株価のある銘柄が一部、
                           または最後の実行が partial）。銘柄ごとの判定では使わない（その銘柄は 3 か 6・7 になる）。
6. fallback                取得元の切り替えによる代替取得（米国株だけ：株価の取得元が provider_chain() の先頭ではない）。
7. ok                      取得成功・最新データあり。

- 「取得日時」（fetched_at・実行の終了時刻）と「株価の基準日」（trade_date）は別の項目として返す。
- 実行の状態のうち running（実行中）・skipped（その時点で未設定）・interrupted（処理の中断）は、取得元の障害の
  判定に使わない。
- 日本株の遅延は、J-Quants のプランの仕様（providers/jquants.py の DELAY_DAYS・DELAY_NOTE）から決める。
  JQUANTS_PLAN が指定されていない場合は、プランを推測せず「プラン未確認」とし、遅延の有無も判定しない
  （どのプランでも古すぎる場合だけ「古いデータ」にする）。
- APIキーの有無は Settings.has_credential()（はい／いいえ）だけで判定し、値は扱わない。
"""
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import JQUANTS_PLANS, get_settings
from ..models import DailyPrice, FinancialFact, IngestionRun, Security
from ..providers import jquants
from .quality import STALE_DAYS

PROVIDER_LABEL = {"jquants": "J-Quants API V2", "tiingo": "Tiingo", "massive": "Massive（旧Polygon.io）"}
DATASET = {"JP": "prices_jp", "US": "prices_us"}
TOLERANCE_DAYS = STALE_DAYS["daily"]  # 週末・祝日を含めた許容日数（遅延のない日足でも数日空くことがある）

# 状態 → 画面の状態（StatusBadge の色分け。DataStatus）
DATA_STATUS = {"not_configured": "not_connected", "configured_not_fetched": "not_connected", "provider_error": "error",
               "stale": "stale", "partial": "ok", "fallback": "ok", "ok": "ok"}


@dataclass
class MarketContext:
    """市場ごとの設定と取得の記録（銘柄ごとの判定で何度も使うので、まとめて1回だけ調べる）。"""
    market: str
    configured: bool
    providers: tuple[str, ...]          # 取得を試す順（米国株は provider_chain()、日本株は jquants）
    last_run: IngestionRun | None       # 最後に終わった取得の実行（success / partial / failed）
    delay_days: int | None              # 提供元の遅延（日）。日本株でプラン未確認なら None
    max_delay_days: int                 # どのプランでも、これより古ければ確実に古い（プラン未確認のときに使う）
    delay_label: str                    # 例「Freeプラン・約12週間遅延」「プラン未確認」「日足（終値）」
    plan_confirmed: bool | None         # 日本株：JQUANTS_PLAN が指定されているか。米国株は None
    notes: list[str] = field(default_factory=list)


def _weeks(days: int) -> str:
    return f"約{days // 7}週間" if days % 7 == 0 else f"約{days}日"


def jp_delay(st) -> tuple[int | None, str, bool]:
    """（遅延日数, 表示, プラン確認済みか）。プランの遅延は providers/jquants.py の仕様の表から決める。"""
    if not st.jquants_plan_specified:
        return None, "プラン未確認（JQUANTS_PLAN 未設定のため遅延を判定できません）", False
    plan = st.jquants_plan
    days = jquants.DELAY_DAYS.get(plan, 0)
    name = plan.capitalize()
    return days, (f"{name}プラン・{_weeks(days)}遅延" if days else f"{name}プラン・前営業日まで"), True


def market_context(session: Session, market: str) -> MarketContext:
    from . import ingest_us_prices  # 取得の処理と同じ判定（provider_chain）を使う
    st = get_settings()
    last = session.scalar(select(IngestionRun).where(IngestionRun.dataset == DATASET[market],
                                                     IngestionRun.status.in_(("success", "partial", "failed")))
                          .order_by(IngestionRun.id.desc()).limit(1))
    if market == "JP":
        days, label, confirmed = jp_delay(st)
        return MarketContext("JP", st.has_credential("jquants"), ("jquants",), last, days,
                             max(jquants.DELAY_DAYS.get(p, 0) for p in JQUANTS_PLANS), label, confirmed)
    chain = tuple(ingest_us_prices.provider_chain(st))
    return MarketContext("US", bool(chain), chain, last, 0, 0, "日足（終値）", None)


def is_stale(ctx: MarketContext, trade_date: date | None, today: date | None = None) -> bool:
    if trade_date is None:
        return True
    age = ((today or date.today()) - trade_date).days
    limit = (ctx.delay_days if ctx.delay_days is not None else ctx.max_delay_days) + TOLERANCE_DAYS
    return age > limit


def target_ids(market: str):
    """取得対象の銘柄（一部取得の分母）。
    日本株：上場中の日本株すべて（J-Quants の日足は日付ごとに全銘柄を返すため。ETF・REIT を含む）。
    米国株：財務データ（SEC）のある米国株（Tiingo が取得する分析対象。Massive の全銘柄の取得もこの範囲で数える）。"""
    if market == "JP":
        return select(Security.id).where(Security.market == "JP", Security.is_active.is_(True))
    us = select(Security.id).where(Security.market == "US")
    return select(FinancialFact.security_id).where(FinancialFact.security_id.in_(us)).distinct()


def is_target(session: Session, s: Security) -> bool:
    if s.market == "JP":
        return bool(s.is_active)
    return session.scalar(select(FinancialFact.id).where(FinancialFact.security_id == s.id).limit(1)) is not None


def counts(session: Session, ctx: MarketContext) -> tuple[int, int]:
    """（取得対象の銘柄数, そのうち株価のある銘柄数）。株価は現在の設定の取得元に限らず、その市場の取得元で数える。"""
    provs = ("jquants",) if ctx.market == "JP" else ("tiingo", "massive")
    t = target_ids(ctx.market).subquery()
    targets = session.scalar(select(func.count()).select_from(t)) or 0
    fetched = session.scalar(select(func.count(func.distinct(DailyPrice.security_id)))
                             .where(DailyPrice.provider.in_(provs), DailyPrice.security_id.in_(select(t.c[0])))) or 0
    return targets, fetched


@dataclass
class PriceState:
    state: str
    label: str
    data_status: str
    note: str
    provider: str | None = None          # 表示している株価の取得元
    as_of: date | None = None            # 株価の基準日（trade_date）
    fetched_at: datetime | None = None   # 取得日時
    close: float | None = None           # 表示する終値（保存済みの値。作らない）


def _outage(ctx: MarketContext) -> str:
    return ("株価の取得元（J-Quants）の障害で最新の取得に失敗しました。日本株の株価に代わりの取得元はありません"
            if ctx.market == "JP" else "株価の取得元の障害で最新の取得に失敗しました（代わりの取得元でも取得できませんでした）")


def _ok_label(ctx: MarketContext) -> str:
    if ctx.market == "JP":
        return f"取得済み（{ctx.delay_label}）" if ctx.plan_confirmed else "取得済み（プラン未確認・遅延を判定できません）"
    return "取得済み"


def _delay_note(ctx: MarketContext) -> str:
    if ctx.market == "JP":
        if not ctx.plan_confirmed:
            return "J-Quants のプランが未確認のため（JQUANTS_PLAN 未設定）、遅延の有無を判定できません。リアルタイムの株価ではありません"
        return f"{jquants.DELAY_NOTE.get(get_settings().jquants_plan, ctx.delay_label)}。リアルタイムの株価ではありません"
    return "日足の終値。リアルタイムの株価ではありません"


def security_state(session: Session, s: Security, ctx: MarketContext, p: DailyPrice | None = None,
                   with_counts: bool = True, lookup: bool = True) -> PriceState:
    """1銘柄の株価の状態。p はその銘柄の最新の株価（lookup=True で未指定なら調べる。検索結果ではまとめて調べて渡す）。"""
    if p is None and lookup:
        p = latest_prices(session, [s.id], ctx.providers).get(s.id)
    shown = dict(provider=PROVIDER_LABEL.get(p.provider, p.provider), as_of=p.trade_date, fetched_at=p.fetched_at,
                 close=p.close) if p else {}
    stored = f"表示は {p.trade_date} の保存済みの終値です（取得 {p.fetched_at:%Y-%m-%d %H:%M} UTC）" if p and p.fetched_at else (
        f"表示は {p.trade_date} の保存済みの終値です" if p else "")
    # 1. APIキー未設定（優先）
    if not ctx.configured:
        what = "J-Quants の APIキー" if s.market == "JP" else "米国株の株価API（Tiingo / Massive）の APIキー"
        if p:
            return PriceState("not_configured", "APIキー未設定", "stale",
                              f"{what}が未設定のため、株価を更新していません。{stored}", **shown)
        return PriceState("not_configured", "APIキー未設定", DATA_STATUS["not_configured"], f"{what}が未設定のため未接続")
    # 2. 取得失敗・API障害
    if ctx.last_run is not None and ctx.last_run.status == "failed":
        note = _outage(ctx) + (f"。{stored}。{_delay_note(ctx)}" if p else "。株価の値は作りません")
        # 画面の色分け：保存済みの株価を基準日つきで示すときは「古いデータ」、株価がなければ「エラー」（PR #4 と同じ）
        return PriceState("provider_error", "取得失敗（API障害）", "stale" if p else "error", note, **shown)
    # 3. 設定済み・未取得
    if p is None:
        note = "APIキーは設定済みですが、この銘柄の株価はまだ取得していません"
        if with_counts:
            targets, fetched = counts(session, ctx)
            if fetched:
                note += f"（取得対象 {targets:,} 銘柄のうち {fetched:,} 銘柄を取得済み）"
            if not is_target(session, s):
                note += ("。この銘柄は株価の取得対象（財務データのある米国株）に含まれていません" if s.market == "US"
                         else "。この銘柄は株価の取得対象（上場中の日本株）に含まれていません")
        return PriceState("configured_not_fetched", "設定済み・未取得", DATA_STATUS["configured_not_fetched"], note)
    # 4. 保存データあり・更新遅延
    if is_stale(ctx, p.trade_date):
        return PriceState("stale", "古いデータ", "stale",
                          f"最新の株価が提供元の遅延（{ctx.delay_label}）を超えて古くなっています。{stored}。{_delay_note(ctx)}", **shown)
    # 6. 取得元の切り替えによる代替取得（米国株）
    if s.market == "US" and ctx.providers and p.provider != ctx.providers[0]:
        if p.provider in ctx.providers:
            return PriceState("fallback", f"代替の取得元で取得（{PROVIDER_LABEL.get(p.provider, p.provider)}）", "ok",
                              f"主の取得元（{PROVIDER_LABEL.get(ctx.providers[0])}）ではなく、代替の取得元"
                              f"（{PROVIDER_LABEL.get(p.provider, p.provider)}）の終値です。{_delay_note(ctx)}", **shown)
        return PriceState("ok", _ok_label(ctx), "ok", f"現在の設定にない取得元（{PROVIDER_LABEL.get(p.provider, p.provider)}）"
                          f"の保存済みの終値です。{_delay_note(ctx)}", **shown)
    # 7. 取得成功・最新データあり
    return PriceState("ok", _ok_label(ctx), "ok", _delay_note(ctx), **shown)


def latest_prices(session: Session, ids: list[int], order: tuple[str, ...] = ()) -> dict[int, DailyPrice]:
    """銘柄ごとの最新の株価。同じ基準日に複数の取得元の株価があれば、order（provider_chain() の順）で先のものを使う。"""
    if not ids:
        return {}
    last = (select(DailyPrice.security_id, func.max(DailyPrice.trade_date).label("d"))
            .where(DailyPrice.security_id.in_(ids)).group_by(DailyPrice.security_id).subquery())
    rank = {p: i for i, p in enumerate(order)}
    out: dict[int, DailyPrice] = {}
    for p in session.scalars(select(DailyPrice).join(last, (DailyPrice.security_id == last.c.security_id)
                                                     & (DailyPrice.trade_date == last.c.d))):
        cur = out.get(p.security_id)
        if cur is None or (rank.get(p.provider, len(rank)), p.provider) < (rank.get(cur.provider, len(rank)), cur.provider):
            out[p.security_id] = p
    return out


@dataclass
class DatasetPriceState:
    state: str
    label: str
    data_status: str
    target_count: int
    fetched_count: int
    used_provider: str | None
    latest_data_date: date | None
    notes: list[str]


def dataset_state(session: Session, market: str) -> DatasetPriceState:
    """市場全体の株価の状態（データの状況ページ）。"""
    ctx = market_context(session, market)
    provs = ("jquants",) if market == "JP" else ("tiingo", "massive")
    latest = session.scalar(select(func.max(DailyPrice.trade_date)).where(DailyPrice.provider.in_(provs)))
    targets, fetched = counts(session, ctx)
    used = None
    if latest is not None:  # 最新の基準日の株価の取得元（同じ日に複数あれば provider_chain() の順で先のもの）
        on_latest = {p for (p,) in session.execute(select(DailyPrice.provider).where(
            DailyPrice.provider.in_(provs), DailyPrice.trade_date == latest).distinct())}
        used = next((p for p in ctx.providers if p in on_latest), sorted(on_latest)[0] if on_latest else None)
    base = dict(target_count=targets, fetched_count=fetched, used_provider=PROVIDER_LABEL.get(used, used) if used else None,
                latest_data_date=latest)
    notes = [f"遅延：{ctx.delay_label}"]
    if not ctx.configured:
        return DatasetPriceState("not_configured", "APIキー未設定", "not_connected", notes=notes + (
            [f"保存済みの株価（{latest} 時点）は更新されません"] if latest else []), **base)
    if ctx.last_run is not None and ctx.last_run.status == "failed":
        return DatasetPriceState("provider_error", "取得失敗（API障害）", "error", notes=notes + [_outage(ctx)], **base)
    if latest is None:
        return DatasetPriceState("configured_not_fetched", "設定済み・未取得", "not_connected",
                                 notes=notes + ["APIキーは設定済みですが、株価はまだ取得していません"], **base)
    if is_stale(ctx, latest):
        return DatasetPriceState("stale", "古いデータ", "stale",
                                 notes=notes + [f"最新の株価（{latest}）が提供元の遅延を超えて古くなっています"], **base)
    if (targets and fetched < targets) or (ctx.last_run is not None and ctx.last_run.status == "partial"):
        why = [] if not (ctx.last_run and ctx.last_run.status == "partial") else ["最後の取得で一部の銘柄が失敗しました"]
        return DatasetPriceState("partial", f"一部取得（{fetched:,}／{targets:,} 銘柄）", "ok", notes=notes + why, **base)
    if market == "US" and used and ctx.providers and used != ctx.providers[0]:
        return DatasetPriceState("fallback", f"代替の取得元で取得（{PROVIDER_LABEL.get(used, used)}）", "ok",
                                 notes=notes + [f"主の取得元（{PROVIDER_LABEL.get(ctx.providers[0])}）ではなく代替の取得元の株価です"],
                                 **base)
    return DatasetPriceState("ok", _ok_label(ctx), "ok", notes=notes, **base)
