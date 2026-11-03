"""テクニカル指標のバックテスト（第3段階の実践課題）。

先読みを防ぐため、シグナルは当日の終値で判定し、翌営業日から保有する（pos.shift(1)）。
売買のたびに片道コストを差し引き、非保有時は現金（米国は3か月物T-Bill、日本は0%）で運用する。

使い方:
    python3 -I tools/backtest.py <FREDのCSVを置いたディレクトリ>

必要なCSV（FRED fredgraph.csv?id=<ID> で取得）: NASDAQCOM, NIKKEI225, SP500, DTB3
注意: いずれも配当を含まない「価格指数」。買い持ちは配当の分だけ不利に評価される
（＝タイミング戦略に有利なバイアス）。
"""
import sys

import numpy as np
import pandas as pd

DAYS = 252


def load(base, sid):
    s = pd.read_csv(f"{base}/{sid}.csv", index_col=0, parse_dates=True).iloc[:, 0]
    return pd.to_numeric(s, errors="coerce").dropna()


# ---------- 指標 ----------
def sma(c, n):
    return c.rolling(n).mean()


def ema(c, n):
    return c.ewm(span=n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def stateful(enter, exit_):
    """enter が真で保有開始、exit が真で手仕舞い（状態を持つシグナル）。"""
    pos, out = 0, []
    for e, x in zip(enter.to_numpy(), exit_.to_numpy()):
        if pos == 0 and e:
            pos = 1
        elif pos == 1 and x:
            pos = 0
        out.append(pos)
    return pd.Series(out, index=enter.index, dtype=float)


# ---------- 戦略（当日終値までの情報で 0/1 を返す） ----------
def strategies(c):
    m, s = ema(c, 12) - ema(c, 26), None
    s = ema(m, 9)
    r = rsi(c)
    mid, sd = sma(c, 20), c.rolling(20).std()
    return {
        "買い持ち": pd.Series(1.0, index=c.index),
        "200日移動平均（終値>SMA200）": (c > sma(c, 200)).astype(float),
        "ゴールデンクロス（SMA50>SMA200）": (sma(c, 50) > sma(c, 200)).astype(float),
        "MACD（MACD>シグナル）": (m > s).astype(float),
        "RSI逆張り（RSI<30で買い、>55で売り）": stateful(r < 30, r > 55),
        "ボリンジャー逆張り（-2σで買い、中心線で売り）": stateful(c < mid - 2 * sd, c > mid),
        "ブレイクアウト（252日高値で買い、63日安値で売り）": stateful(c >= c.rolling(252).max(), c <= c.rolling(63).min()),
    }


# ---------- 評価 ----------
def run(c, rf, sig, cost):
    r = c.pct_change().fillna(0)
    pos = sig.shift(1).fillna(0)  # 翌日から保有（先読み防止）
    trades = pos.diff().abs().fillna(0)
    ret = pos * r + (1 - pos) * rf - trades * cost
    return ret, pos, trades


def metrics(ret, rf, pos, trades):
    eq = (1 + ret).cumprod()
    yrs = len(ret) / DAYS
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = ret.std() * np.sqrt(DAYS)
    ex = ret - rf
    sharpe = ex.mean() / ex.std() * np.sqrt(DAYS) if ex.std() > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    return {
        "CAGR": cagr, "ボラ": vol, "シャープ": sharpe, "最大DD": dd,
        "保有率": pos.mean(), "売買回数/年": trades.sum() / yrs,
    }


def table(c, rf, cost, start=None, end=None):
    sigs = strategies(c)  # 指標は全期間で計算（過去データのみ使用）してから期間を切り出す
    rows = {}
    r_mkt = c.pct_change().fillna(0)
    for name, sig in sigs.items():
        ret, pos, tr = run(c, rf, sig, cost)
        sl = slice(start, end)
        m = metrics(ret[sl], rf[sl], pos[sl], tr[sl])
        if name != "買い持ち":
            # 同じ平均保有率で常に一定比率を持つ「保有率一致ベンチマーク」との差
            e = pos[sl].mean()
            bench = e * r_mkt[sl] + (1 - e) * rf[sl]
            diff = ret[sl] - bench
            m["対一致BM 超過/年"] = diff.mean() * DAYS
            m["t値"] = diff.mean() / diff.std() * np.sqrt(len(diff))
        rows[name] = m
    return pd.DataFrame(rows).T


def sweep(c, rf, cost, ns, is_end, oos_start):
    out = []
    for n in ns:
        sig = (c > sma(c, n)).astype(float)
        ret, pos, tr = run(c, rf, sig, cost)
        a = metrics(ret[:is_end], rf[:is_end], pos[:is_end], tr[:is_end])
        b = metrics(ret[oos_start:], rf[oos_start:], pos[oos_start:], tr[oos_start:])
        out.append({"SMA期間": n, "IS シャープ": a["シャープ"], "IS CAGR": a["CAGR"],
                    "OOS シャープ": b["シャープ"], "OOS CAGR": b["CAGR"]})
    return pd.DataFrame(out).set_index("SMA期間")


def md(df):
    pct = {"CAGR", "ボラ", "最大DD", "保有率", "対一致BM 超過/年", "IS CAGR", "OOS CAGR"}
    cols = list(df.columns)
    lines = ["| " + " | ".join([df.index.name or "戦略"] + cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for i, row in df.iterrows():
        cells = []
        for k in cols:
            v = row[k]
            if pd.isna(v):
                cells.append("—")
            elif k in pct:
                cells.append(f"{v*100:.1f}%")
            else:
                cells.append(f"{v:.2f}")
        lines.append(f"| {i} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    base = sys.argv[1]
    tb = load(base, "DTB3")
    for sid, us, is_end, oos in [("NASDAQCOM", True, "1999-12-31", "2000-01-01"),
                                 ("NIKKEI225", False, "1989-12-31", "1990-01-01"),
                                 ("SP500", True, None, None)]:
        c = load(base, sid)
        rf = ((1 + tb.reindex(c.index, method="ffill") / 100) ** (1 / DAYS) - 1) if us else pd.Series(0.0, index=c.index)
        print(f"\n## {sid}  {c.index[0].date()} 〜 {c.index[-1].date()}  （現金金利：{'3か月T-Bill' if us else '0%'}）")
        periods = [("全期間", None, None)] if is_end is None else [
            ("全期間", None, None), (f"前半（〜{is_end[:4]}）", None, is_end), (f"後半（{oos[:4]}〜）", oos, None)]
        for cost in (0.001, 0.003):
            for label, a, b in periods:
                print(f"\n### {label}・片道コスト{cost*100:.1f}%\n")
                print(md(table(c, rf, cost, a, b)))
        if is_end:
            print(f"\n### パラメータ最適化の検証：SMA期間（前半で選び、後半で評価）・コスト0.1%\n")
            print(md(sweep(c, rf, 0.001, [20, 30, 50, 75, 100, 125, 150, 175, 200, 250, 300], is_end, oos)))
