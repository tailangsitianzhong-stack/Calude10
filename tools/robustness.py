"""第10段階：これまでの主要な結論の頑健性を再検証する。

使い方:
    python3 -I tools/robustness.py <SEC framesのディレクトリ> <日次指数のディレクトリ> <French 5ファクター等のディレクトリ> <マクロのディレクトリ>

1. 第6段階の基準率を、別のスクリーニング時点（2013年初め：2009→2012年度の成長 → 2012→2022年度）で再現する。
2. 第3段階の主要な t 値を、自己相関・不均一分散に頑健な Newey-West の t 値で再計算する。
3. 第5段階のファクターと第4段階のインフレ局面の差を、同じく Newey-West で再計算する。
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import backtest as BT  # noqa: E402
from growth_screen import frame, revenue  # noqa: E402
from macro_analysis import french_block, fred  # noqa: E402


def nw_t(x, lags):
    """平均がゼロという帰無仮説に対する Newey-West（Bartlett カーネル）の t 値。"""
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(x)
    e = x - x.mean()
    s = e @ e / n
    for l in range(1, lags + 1):
        w = 1 - l / (lags + 1)
        s += 2 * w * (e[l:] @ e[:-l]) / n
    return x.mean() / np.sqrt(s / n)


def simple_t(x):
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * np.sqrt(len(x))


def screen(base, S):
    """スクリーニング年度 S（その翌年初めに判断）。過去3年＝S-3→S、将来10年＝S→S+10。"""
    r0, _ = revenue(base, S - 3)
    r1, _ = revenue(base, S - 1)
    rS, _ = revenue(base, S)
    rF, _ = revenue(base, S + 10)
    rF1, _ = revenue(base, S + 9)
    op, _ = frame(base, f"OperatingIncomeLoss_CY{S}")
    df = pd.DataFrame({"r0": r0, "r1": r1, "rS": rS, "rF": rF, "rF1": rF1, "op": op})
    u0 = df[(df["rS"] >= 1e8) & (df["r0"] > 1e6)]
    u = u0[(u0["rS"] / u0["r1"]).between(0.5, 2) & (u0["r1"] / u0["r0"]).between(0.1, 10)].copy()
    bad = u["rF"].notna() & ~(u["rF"] / u["rF1"]).between(0.5, 2)
    u.loc[bad, "rF"] = np.nan
    u["past"] = (u["rS"] / u["r0"]) ** (1 / 3) - 1
    u["fut"] = (u["rF"] / u["rS"]) ** (1 / 10) - 1
    u["om"] = u["op"] / u["rS"]
    alive = u[u["rF"].notna()]
    big = u[u["rS"] >= 5e8]
    A = u[u["past"] >= 0.25]
    C = A[A["om"] < 0]
    q5 = u[u["past"] >= u["past"].quantile(0.8)]
    return {
        "スクリーニング時点": f"{S+1}年初め（{S-3}→{S}年度 → {S}→{S+10}年度）",
        "母集団（社）": len(u),
        "10年後に売上の報告あり": u["rF"].notna().mean(),
        "基準率：年15%以上を10年継続": (u["fut"] >= 0.15).mean(),
        "過去成長の上位20%：同": (q5["fut"] >= 0.15).mean(),
        "高成長（過去CAGR≥25%）：同": (A["fut"] >= 0.15).mean(),
        "高成長＋赤字：生存率": C["rF"].notna().mean() if len(C) else np.nan,
        "過去と将来の成長率の順位相関": alive["past"].rank().corr(alive["fut"].rank()),
        "売上5億ドル超で年20%以上（勝者の割合）": (big["fut"] >= 0.20).mean(),
        "勝者のうち高成長スクリーンに入っていた割合": (big[big["fut"] >= 0.20]["past"] >= 0.25).mean(),
    }


def main(sec, px, ff5dir, mdir):
    print("## 1. 第6段階の基準率の再現（2つのスクリーニング時点）\n")
    rows = {S: screen(sec, S) for S in (2012, 2015)}
    keys = list(rows[2012].keys())
    print("| 指標 | " + " | ".join(rows[S][keys[0]] for S in rows) + " |\n|---|" + "---|" * len(rows))
    for k in keys[1:]:
        cells = []
        for S in rows:
            v = rows[S][k]
            cells.append(f"{v:,}" if isinstance(v, (int, np.integer)) else ("—" if pd.isna(v) else (f"{v:.3f}" if "相関" in k else f"{v*100:.1f}%")))
        print(f"| {k} | " + " | ".join(cells) + " |")
    print()

    print("## 2. 第3段階の主要な t 値の再計算（日次、対・保有率一致ベンチマーク、Newey-West ラグ20日）\n")
    print("| 指数・戦略・期間 | 単純な t 値 | Newey-West の t 値 |\n|---|---|---|")
    for sid, a, b in [("NASDAQCOM", None, "1999-12-31"), ("NASDAQCOM", "2000-01-01", None), ("NIKKEI225", None, "1989-12-31"), ("NIKKEI225", "1990-01-01", None)]:
        c = BT.load(px, sid)
        rf = pd.Series(0.0, index=c.index)
        sigs = BT.strategies(c)
        r_mkt = c.pct_change().fillna(0)
        for name in ["200日移動平均（終値>SMA200）", "MACD（MACD>シグナル）"]:
            ret, pos, _ = BT.run(c, rf, sigs[name], 0.001)
            sl = slice(a, b)
            e = pos[sl].mean()
            diff = ret[sl] - (e * r_mkt[sl] + (1 - e) * rf[sl])
            per = f"{'〜'+b[:4] if b else a[:4]+'〜'}"
            print(f"| {sid}・{name.split('（')[0]}・{per} | {simple_t(diff):.2f} | {nw_t(diff, 20):.2f} |")
    print("\n（注：第3段階では米国の現金をT-Billとしたが、ここでは比較のため両市場とも現金0%で計算）\n")

    print("## 3. 第5段階のファクターと第4段階のインフレ局面（月次、Newey-West ラグ12か月）\n")
    f5 = french_block(f"{ff5dir}/F-F_Research_Data_5_Factors_2x3.csv", "Mkt-RF")
    mom = french_block(f"{ff5dir}/F-F_Momentum_Factor.csv", ",Mom")
    F = pd.concat([f5, mom], axis=1)
    print("| 系列・期間 | 単純な t 値 | Newey-West の t 値 |\n|---|---|---|")
    for c, a, b in [("HML", "2000-01", None), ("Mom", "2000-01", None), ("RMW", "2000-01", None), ("HML", "1963-07", "1999-12"), ("Mom", "1963-07", "1999-12")]:
        x = F[c].loc[a:b]
        print(f"| {c} {a[:4]}〜{(b or '2026')[:4]} | {simple_t(x):.2f} | {nw_t(x, 12):.2f} |")
    ff3 = french_block(f"{mdir}/ff/F-F_Research_Data_Factors.csv", "Mkt-RF")
    cpi = fred(mdir, "CPIAUCSL")
    infl = (cpi / cpi.shift(12) - 1).shift(2)
    m = ff3["Mkt-RF"].loc["1955-01":]
    hi = (infl.reindex(m.index) >= 0.04)
    # 回帰 m = a + b×高インフレのダミー。b の t 値を、通常の最小二乗法と Newey-West の標準誤差で比較
    d = m[hi].mean() - m[~hi].mean()
    x = hi.astype(float).to_numpy()
    y = m.to_numpy()
    X = np.column_stack([np.ones_like(x), x])
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    e = y - X @ beta
    n, L = len(y), 12
    S = np.zeros((2, 2))
    for l in range(0, L + 1):
        w = 1 if l == 0 else 2 * (1 - l / (L + 1))
        G = (X[l:] * e[l:, None]).T @ (X[:n - l] * e[:n - l, None]) / n
        S += w * (G + G.T) / 2 if l else G
    Q = np.linalg.inv(X.T @ X / n)
    V = Q @ S @ Q / n
    t_nw = beta[1] / np.sqrt(V[1, 1])
    t_ols = beta[1] / np.sqrt((e @ e / (n - 2)) * np.linalg.inv(X.T @ X)[1, 1])
    print(f"| 市場超過リターン：高インフレ（≧4%）−低インフレ（年率 {d*12*100:.1f}%） | {t_ols:.2f} | {t_nw:.2f} |")
    print()


if __name__ == "__main__":
    main(*sys.argv[1:5])
