"""データ品質の検証（欠損・古さ・異常値）。設計書 8.1・第10段階の教訓（データの誤りが最も多かった）に基づく。"""
from datetime import date, timedelta
from statistics import pstdev

# 系列の頻度ごとの「古い」と判定する経過日数（最新の観測日から今日まで）
STALE_DAYS = {"daily": 7, "weekly": 21, "monthly": 75, "quarterly": 200}


def staleness(last_obs: date | None, frequency: str, today: date | None = None) -> tuple[bool, int | None]:
    if last_obs is None:
        return True, None
    today = today or date.today()
    age = (today - last_obs).days
    return age > STALE_DAYS.get(frequency, 7), age


def outliers(values: list[tuple[date, float]], z: float = 8.0, window: int = 250) -> list[dict]:
    """直近の変化率が、過去の変化率の標準偏差の z 倍を超えたものを異常値の候補とする。
    （本物の急変もありうるので、削除はせず「警告」として記録する）"""
    if len(values) < 30:
        return []
    vals = values[-window:]
    chg = [(vals[i][0], vals[i][1] / vals[i - 1][1] - 1) for i in range(1, len(vals)) if vals[i - 1][1] not in (0,)]
    if len(chg) < 20:
        return []
    # 判定する直近5件を除いた期間で、通常の変動の大きさを測る（異常値自身で基準が膨らむのを防ぐ）
    sd = pstdev([c for _, c in chg[:-5]]) or 0.0
    if sd == 0:
        return []
    return [{"date": d.isoformat(), "change": round(c, 6), "z": round(abs(c) / sd, 1)} for d, c in chg[-5:] if abs(c) > z * sd]


# ---- 品質の記録の重複防止（2026-11-04：同じ開示日を取り直したとき「銘柄一覧にない」が110件重複した問題） ----

def add_once(session, dataset: str, subject: str, check_type: str, identity: dict, severity: str, detail: dict) -> bool:
    """同じ記録（dataset・subject・check_type と、identity に指定した detail の値がすべて同じもの）がなければ追加する。
    identity の例：{"disc_no": ...}（開示）、{"doc_id": ..., "concept": ...}（EDINET の書類と項目）。
    同じ実行の中でまだ保存していない記録も重複として扱う（session.info に覚える）。追加したら True。"""
    from sqlalchemy import select
    from ..models import DataQualityCheck
    key = (dataset, subject, check_type, tuple(sorted((k, str(v)) for k, v in identity.items())))
    seen = session.info.setdefault("quality_keys", set())
    if key in seen:
        return False
    for d in session.scalars(select(DataQualityCheck.detail).where(
            DataQualityCheck.dataset == dataset, DataQualityCheck.subject == subject, DataQualityCheck.check_type == check_type)):
        if all(str((d or {}).get(k)) == str(v) for k, v in identity.items()):
            seen.add(key)
            return False
    session.add(DataQualityCheck(dataset=dataset, subject=subject, check_type=check_type, severity=severity, detail=detail))
    seen.add(key)
    return True


def forget_pending(session) -> None:
    """ロールバックしたときに、保存されなかった記録を「記録済み」と覚えたままにしない。"""
    session.info.pop("quality_keys", None)


def remove_duplicates(session, dataset: str | None = None) -> int:
    """内容がまったく同じ品質の記録（dataset・subject・check_type・detail が同じ）の重複を削除し、最も古い1件を残す。
    品質の記録だけが対象（財務の値には触れない）。戻り値は削除した件数。"""
    import json
    from sqlalchemy import delete, select
    from ..models import DataQualityCheck
    q = select(DataQualityCheck.id, DataQualityCheck.dataset, DataQualityCheck.subject, DataQualityCheck.check_type,
               DataQualityCheck.detail).order_by(DataQualityCheck.id)
    if dataset:
        q = q.where(DataQualityCheck.dataset == dataset)
    keep, drop = set(), []
    for i, ds, sub, ct, det in session.execute(q):
        k = (ds, sub, ct, json.dumps(det, sort_keys=True, ensure_ascii=False))
        if k in keep:
            drop.append(i)
        else:
            keep.add(k)
    for chunk in range(0, len(drop), 500):
        session.execute(delete(DataQualityCheck).where(DataQualityCheck.id.in_(drop[chunk:chunk + 500])))
    session.commit()
    return len(drop)
