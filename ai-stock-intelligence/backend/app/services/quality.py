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
