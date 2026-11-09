"""J-Quants のプランの表示と TOPIX の状態（画面・API の表示用。取得の処理は Settings.jquants_fetch_plan を使う）。

プランの状態（Settings.jquants_plan_status）：
- confirmed：対応するプラン（free / light / standard / premium）が指定されている → そのプランの仕様で遅延・利用条件を示す
- unset：JQUANTS_PLAN が未設定 → 「プラン未確認」。Free と表示しない（取得は Free 相当の安全側の条件で行う）
- invalid：対応していない値 → 「設定不備」。Free に置き換えない。J-Quants の取得は止める（ほかのデータ連携は通常どおり）
プランの仕様（遅延・TOPIX の提供範囲）は providers/jquants.py の表から決め、ここでは固定しない。
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import JQUANTS_PLANS
from ..models import DataQualityCheck, IndexBar
from ..providers import jquants
from .quality import STALE_DAYS

RUN_RECORD = "jquants_run"  # 取得の実行ごとの構造化した記録（品質の記録の表の JSON の列。DB の構造は変えない）


def plan_name(plan: str) -> str:
    return plan.capitalize()


def delay_days(st) -> int | None:
    """確認済みのプランの遅延（日）。未設定・不正なら None（推測しない）。"""
    return jquants.DELAY_DAYS.get(st.jquants_plan, 0) if st.jquants_plan_status == "confirmed" else None


def max_delay_days() -> int:
    """どのプランでも、これより古ければ確実に古い（プランが分からないときの古さの判定に使う）。"""
    return max(jquants.DELAY_DAYS.get(p, 0) for p in JQUANTS_PLANS)


def _weeks(days: int) -> str:
    return f"約{days // 7}週間" if days % 7 == 0 else f"約{days}日"


def delay_label(st) -> str:
    """遅延の表示（例「Freeプラン・約12週間遅延」「プラン未確認（JQUANTS_PLAN 未設定）」「設定不備（…）」）。"""
    if st.jquants_plan_status == "invalid":
        return "設定不備（JQUANTS_PLAN の値が不正のため遅延を判定できません）"
    if st.jquants_plan_status != "confirmed":
        return "プラン未確認（JQUANTS_PLAN 未設定のため遅延を判定できません）"
    days = delay_days(st)
    return f"{plan_name(st.jquants_plan)}プラン・{_weeks(days)}遅延" if days else f"{plan_name(st.jquants_plan)}プラン・前営業日まで"


def invalid_message(st) -> str:
    return (f"JQUANTS_PLAN の設定値「{st.jquants_plan_invalid_value}」は対応していません（設定不備）。J-Quants の取得を停止しています。"
            f"{' / '.join(JQUANTS_PLANS)} のいずれかを設定してください")


def unset_fetch_note() -> str:
    # プランが未確認のときに Free の遅延の期間を表示に流用しない（取得の条件が Free 相当であることだけを示す）
    return ("取得は Free 相当の安全側の条件で行っているため、ご契約のプランで取得できる直近のデータを"
            "取得していない可能性があります")


def run_source_label(st) -> str:
    """取得の実行記録に残す取得条件の名前。未設定を「free」と記録しない。"""
    if st.jquants_plan_status == "confirmed":
        return st.jquants_plan
    return "プラン未設定・Free相当の条件" if st.jquants_plan_status == "unset" else "設定不備"


def financials_delay_note(st) -> str | None:
    """日本企業の財務（決算短信）の注意書き。遅れがない確認済みのプランでは None。"""
    if st.jquants_plan_status == "invalid":
        return invalid_message(st) + "。決算短信の反映の遅れの有無を判定できません"
    if st.jquants_plan_status != "confirmed":
        return ("J-Quants のプランが未確認のため（JQUANTS_PLAN 未設定）、決算短信の反映の遅れの有無を判定できません。"
                + unset_fetch_note())
    days = delay_days(st)
    if days:
        return (f"{plan_name(st.jquants_plan)}プランのため、決算短信は開示から{_weeks(days)}後にしか取得できません。"
                "最新の決算が反映されていない場合があります")
    return None


# ---- TOPIX ----

def save_topix_result(session: Session, run_id: int, result: str, rows: int = 0, error: str | None = None,
                      plan_status: str | None = None) -> None:
    """TOPIX の取得の結果を構造化して残す。result：ok／plan_not_available（API がプランの範囲外と応答）／error。"""
    from datetime import datetime, timezone
    session.add(DataQualityCheck(dataset=RUN_RECORD, subject=str(run_id), check_type="topix_result",
                                 severity="info" if result == "ok" else "warn",
                                 detail={"run_id": run_id, "result": result, "rows": rows, "error": (error or "")[:200] or None,
                                         "plan_status": plan_status, "at": datetime.now(timezone.utc).isoformat()}))


def last_topix_result(session: Session) -> dict | None:
    return session.scalar(select(DataQualityCheck.detail).where(DataQualityCheck.dataset == RUN_RECORD,
                                                                DataQualityCheck.check_type == "topix_result")
                          .order_by(DataQualityCheck.id.desc()).limit(1))


def topix_spec(st) -> dict | None:
    """確認済みのプランの TOPIX の提供範囲（出典つき）。未設定・不正なら None。"""
    return jquants.TOPIX_BY_PLAN.get(st.jquants_plan) if st.jquants_plan_status == "confirmed" else None


def topix_state(session: Session, st, last_bar: IndexBar | None) -> tuple[str, str, str]:
    """TOPIX の状態（state, data_status, note）。上から順に判定する：
    not_configured → plan_invalid →（データあり：ok / stale）→ plan_not_available（API の応答）→ fetch_error →
    plan_excludes（プランの仕様）→ plan_unconfirmed → not_fetched"""
    rec = last_topix_result(session) or {}
    at = (rec.get("at") or "")[:10]
    if not st.has_credential("jquants"):
        return "not_configured", "not_connected", "J-Quants の APIキー未設定のため未接続"
    if st.jquants_plan_status == "invalid":
        note = invalid_message(st)
        if last_bar is not None:
            return "plan_invalid", "stale", f"{note}。表示は {last_bar.trade_date} の保存済みの終値です"
        return "plan_invalid", "error", note
    if last_bar is not None:
        days = delay_days(st)
        limit = (days if days is not None else max_delay_days()) + STALE_DAYS["daily"]
        stale = (date.today() - last_bar.trade_date).days > limit
        note = jquants.DELAY_NOTE.get(st.jquants_plan) if st.jquants_plan_status == "confirmed" else delay_label(st)
        if rec.get("result") in ("plan_not_available", "error"):
            note += f"。最新の取得は{'プランの範囲外と応答' if rec['result'] == 'plan_not_available' else '失敗'}（{at}）"
        return ("stale" if stale else "ok"), ("stale" if stale else "ok"), note
    if rec.get("result") == "plan_not_available":
        return ("plan_not_available", "not_connected",
                f"ご契約のプランでは TOPIX は提供されていません（API の応答、{at}）")
    if rec.get("result") == "error":
        return "fetch_error", "error", f"TOPIX の取得に失敗しました（{at}）。値は作りません"
    spec = topix_spec(st)
    if spec is not None and not spec["available"]:
        return ("plan_excludes", "not_connected",
                f"{plan_name(st.jquants_plan)}プランでは TOPIX は提供されていません（{spec['evidence']}）")
    if spec is None:
        return ("plan_unconfirmed", "not_connected",
                "TOPIX は未取得です。J-Quants のプランが未確認のため（JQUANTS_PLAN 未設定）、提供範囲かどうか判定できません")
    return ("not_fetched", "not_connected",
            f"TOPIX は未取得です（{plan_name(st.jquants_plan)}プランは提供範囲：{spec['evidence']}）")
