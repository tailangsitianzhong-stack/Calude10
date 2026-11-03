"""リスク管理の定量検証（第8段階の実践課題）。

使い方:
    python3 -I tools/risk_analysis.py <マクロのディレクトリ(FRED+ff/)> <日次指数のディレクトリ(FRED)>

データ:
  - French: F-F_Research_Data_Factors（米国株の月次総リターン=Mkt-RF+RF, T-Bill=RF）, 12_Industry_Portfolios
  - FRED: GS10（10年国債利回り、月平均）, DEXJPUS（ドル円）, IRSTCI01JPM156N（日本のコールレート）, NASDAQCOM, NIKKEI225（日次）
10年国債の月次リターンは、利回りから「前月の利回りをクーポンとする10年債の価格変化＋利息」で合成した近似値
（GS10は月平均の利回りなので、月末値に比べ変動がやや平滑化される）。
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from macro_analysis import french_block, fred, md  # noqa: E402

RNG = np.random.default_rng(20261103)


def bond_returns(y):
    """y: 年率小数の10年国債利回り（月次）。半年複利の10年債で月次総リターンを近似。"""
    c = y.shift(1)
    n = 10 - 1 / 12
    yy = y
    price = c / yy * (1 - (1 + yy / 2) ** (-2 * n)) + (1 + yy / 2) ** (-2 * n)
    return (price - 1 + c / 12).dropna()


def dd_stats(r):
    eq = (1 + r).cumprod()
    peak = eq.cummax()
    dd = eq / peak - 1
    trough = dd.idxmin()
    pk = eq[:trough].idxmax()
    rec = eq[trough:][eq[trough:] >= eq[pk]]
    rec_date = rec.index[0] if len(rec) else None
    return dd.min(), pk, trough, rec_date


def months_between(a, b):
    return (b - a).n if b is not None else None


def port_stats(r, rf):
    eq = (1 + r).cumprod()
    mdd, pk, tr, rc = dd_stats(r)
    ex = r - rf.reindex(r.index)
    q = r.quantile(0.05)
    roll12 = (1 + r).rolling(12).apply(np.prod, raw=True) - 1
    return {
        "年率リターン": eq.iloc[-1] ** (12 / len(r)) - 1, "ボラ": r.std() * np.sqrt(12),
        "シャープ": ex.mean() / ex.std() * np.sqrt(12), "最大DD": mdd,
        "最大DDの期間": f"{pk}→{tr}", "回復までの月数（ピークから）": str(months_between(pk, rc)) if rc is not None else "未回復",
        "最悪の12か月": roll12.min(), "月次VaR 95%": q, "月次CVaR 95%": r[r <= q].mean(),
    }


def main(mdir, pxdir):
    ff3 = french_block(f"{mdir}/ff/F-F_Research_Data_Factors.csv", "Mkt-RF")
    stk = ff3["Mkt-RF"] + ff3["RF"]
    rf = ff3["RF"]
    y = fred(mdir, "GS10") / 100
    bnd = bond_returns(y)
    idx = stk.index.intersection(bnd.index)
    stk, bnd, rfm = stk[idx], bnd[idx], rf[idx]

    # ---------- 1. 資産配分とドローダウン ----------
    ports = {
        "米国株100%": stk, "株80/債20": 0.8 * stk + 0.2 * bnd, "株60/債40（毎月リバランス）": 0.6 * stk + 0.4 * bnd,
        "株40/債60": 0.4 * stk + 0.6 * bnd, "米10年国債100%（合成）": bnd, "T-Bill 100%": rfm,
    }
    print(f"## 1. 資産配分別のリスク（月次、{idx[0]}〜{idx[-1]}、米ドル建て、コスト・税なし）\n")
    t = pd.DataFrame({k: port_stats(v, rfm) for k, v in ports.items()}).T
    t.index.name = "ポートフォリオ"
    print(md(t, pct_cols=["年率リターン", "ボラ", "最大DD", "最悪の12か月", "月次VaR 95%", "月次CVaR 95%"]) + "\n")

    # ---------- 2. ストレステスト：歴史的局面 ----------
    episodes = [("1973-01", "1974-09", "1973〜74年 石油危機・スタグフレーション"), ("1987-09", "1987-11", "1987年 ブラックマンデー"),
                ("2000-09", "2002-09", "2000〜02年 ITバブル崩壊"), ("2007-11", "2009-02", "2007〜09年 世界金融危機"),
                ("2020-02", "2020-03", "2020年 コロナショック"), ("2022-01", "2022-09", "2022年 インフレ・急速な利上げ")]
    rows = {}
    for a, b, name in episodes:
        rows[name] = {k: (1 + v.loc[a:b]).prod() - 1 for k, v in ports.items() if k != "T-Bill 100%"}
    t = pd.DataFrame(rows).T
    t.index.name = "局面（月次の累積リターン）"
    print("## 2. ストレステスト：歴史的局面での累積リターン\n")
    print(md(t, pct_cols=list(t.columns)) + "\n")

    # ---------- 3. 分散効果は危機で弱まる ----------
    ind = french_block(f"{mdir}/ff/12_Industry_Portfolios.csv", "Average Value Weighted Returns -- Monthly").loc["1955-01":]
    m = stk.reindex(ind.index)
    calm, stress = ind[m > -0.03], ind[m <= -0.05]

    def avg_corr(df):
        c = df.corr().to_numpy()
        return c[np.triu_indices_from(c, 1)].mean()
    sb = pd.concat([stk, bnd], axis=1).dropna()
    sb.columns = ["s", "b"]
    print("## 3. 危機のときの相関\n")
    print("### 3a. 市場のリターンで月を分けた場合（条件付き相関）\n")
    print("| 期間の区分 | 月数 | 12業種間の平均相関 | 株と国債の相関 |\n|---|---|---|---|")
    for name, msk in [("平時（市場の月次リターン > −3%）", m > -0.03), ("ストレス時（市場 ≤ −5%）", m <= -0.05)]:
        mm = msk[msk].index
        cs = sb.loc[sb.index.intersection(mm)]
        print(f"| {name} | {len(mm)} | {avg_corr(ind.loc[mm]):.2f} | {cs['s'].corr(cs['b']):.2f} |")
    print("\n（注：市場のリターンで標本を切ると、市場要因の分散が小さくなり相関が機械的に下がる。Forbes & Rigobon 2002 の指摘する条件付きの偏り）\n")
    print("### 3b. 36か月の移動窓で見た場合（危機を含む窓と、全期間の分布）\n")
    roll = []
    for i in range(36, len(ind) + 1):
        w = ind.iloc[i - 36:i]
        roll.append((w.index[-1], avg_corr(w), m.iloc[i - 36:i].std() * np.sqrt(12)))
    rc = pd.DataFrame(roll, columns=["end", "corr", "vol"]).set_index("end")
    print(f"全期間の36か月窓の平均業種間相関：中央値 {rc['corr'].median():.2f}（10%点 {rc['corr'].quantile(0.1):.2f}、90%点 {rc['corr'].quantile(0.9):.2f}）。"
          f"窓の市場ボラとの相関：**{rc['corr'].corr(rc['vol']):.2f}**\n")
    print("| 36か月窓の終わり | 含まれる局面 | 業種間の平均相関 | 市場のボラ（年率） |\n|---|---|---|---|")
    for e, lab in [("1974-12", "1973〜74年"), ("1988-12", "1987年"), ("2002-12", "2000〜02年"), ("2009-12", "2007〜09年"),
                   ("2020-12", "2020年"), ("2017-12", "（平穏期の例）2015〜17年")]:
        x = rc.loc[pd.Period(e, "M")]
        print(f"| {e} | {lab} | {x['corr']:.2f} | {x['vol']*100:.1f}% |")
    print()
    k = np.array([1, 2, 5, 10, 20, 50, 100, 1000])
    sig, rho = 0.30, 0.25
    vol = sig * np.sqrt(1 / k + (1 - 1 / k) * rho)
    print("理論：個別株のボラ30%・平均相関0.25のとき、等金額でn銘柄持つポートフォリオのボラ\n")
    print("| 銘柄数 | " + " | ".join(str(x) for x in k) + " |\n|---|" + "---|" * len(k))
    print("| ボラ | " + " | ".join(f"{v*100:.1f}%" for v in vol) + " |")
    print(f"\n→ 下限は σ×√ρ ＝ {sig*np.sqrt(rho)*100:.1f}%。銘柄を増やしても市場全体のリスクは消せない。\n")

    # ---------- 4. 為替リスク（日本の投資家が米国株を持つ場合） ----------
    fx = fred(mdir, "DEXJPUS")
    jr = fred(mdir, "IRSTCI01JPM156N") / 100
    dfx = fx.pct_change()
    a = pd.concat([stk, dfx, rfm, jr.shift(1) / 12], axis=1).dropna()
    a.columns = ["usd", "fx", "rf_us", "rf_jp"]
    a = a.loc["1986-01":]
    unh = (1 + a["usd"]) * (1 + a["fx"]) - 1
    hed = a["usd"] + a["rf_jp"] - a["rf_us"]  # 金利平価にもとづく為替ヘッジの近似
    print(f"## 4. 為替リスク：日本の投資家から見た米国株（{a.index[0]}〜{a.index[-1]}、円建て）\n")
    rows = {"米ドル建て（参考）": port_stats(a["usd"], a["rf_us"]), "円建て・為替ヘッジなし": port_stats(unh, a["rf_jp"]),
            "円建て・為替ヘッジあり（近似）": port_stats(hed, a["rf_jp"])}
    t = pd.DataFrame(rows).T
    t.index.name = "保有形態"
    print(md(t, pct_cols=["年率リターン", "ボラ", "最大DD", "最悪の12か月", "月次VaR 95%", "月次CVaR 95%"]) + "\n")
    print(f"米国株（ドル建て）とドル円の月次相関：{a['usd'].corr(a['fx']):.2f}（正＝株高のとき円安になりやすい）。"
          f"ヘッジコスト（日米短期金利差）の平均：年{(a['rf_us'] - a['rf_jp']).mean()*12*100:.1f}%\n")

    # ---------- 5. 裾の厚さ ----------
    print("## 5. 裾の厚さ：日次リターンの大きな変動は正規分布の予想よりはるかに多い\n")
    print("| 指数 | 日数 | 尖度（正規分布=0） | 最悪の日 | ±4σを超えた日数 | 正規分布での期待日数 | ±5σ超 | 正規分布での期待日数 |\n|---|---|---|---|---|---|---|---|")
    from math import erfc, sqrt
    for sid in ["NASDAQCOM", "NIKKEI225"]:
        c = pd.read_csv(f"{pxdir}/{sid}.csv", index_col=0, parse_dates=True).iloc[:, 0]
        r = pd.to_numeric(c, errors="coerce").dropna().pct_change().dropna()
        z = (r - r.mean()) / r.std()
        p4, p5 = erfc(4 / sqrt(2)), erfc(5 / sqrt(2))
        print(f"| {sid} | {len(r):,} | {r.kurt():.1f} | {r.min()*100:.1f}%（{r.idxmin().date()}） | {(z.abs() > 4).sum()} | {p4*len(r):.1f} | {(z.abs() > 5).sum()} | {p5*len(r):.3f} |")
    print()

    # ---------- 6. ブロック・ブートストラップ：20年後の資産分布 ----------
    def boot(r, years=20, n=20000, block=12):
        arr = r.to_numpy()
        nb = years * 12 // block
        starts = RNG.integers(0, len(arr) - block, size=(n, nb))
        paths = np.stack([arr[s:s + block] for s in starts.ravel()]).reshape(n, nb * block)
        eq = np.cumprod(1 + paths, axis=1)
        mdd = (eq / np.maximum.accumulate(eq, axis=1) - 1).min(axis=1)
        return eq[:, -1], mdd
    print("## 6. ブロック・ブートストラップ（12か月ブロック、20,000回）：20年後の資産倍率と途中の最大下落\n")
    print("| ポートフォリオ | 資産倍率 5%点 | 中央値 | 95%点 | 元本割れの確率 | 途中の最大DD 中央値 | 最大DD −50%超の確率 |\n|---|---|---|---|---|---|---|")
    for k_, r in [("米国株100%", stk), ("株60/債40", 0.6 * stk + 0.4 * bnd)]:
        fin, mdd = boot(r)
        print(f"| {k_} | {np.percentile(fin,5):.2f} | {np.median(fin):.2f} | {np.percentile(fin,95):.2f} | {(fin<1).mean()*100:.1f}% | "
              f"{np.median(mdd)*100:.1f}% | {(mdd<-0.5).mean()*100:.1f}% |")
    print("\n（過去の月次リターンを12か月単位で再標本化。過去にない種類の危機は含まれない）\n")

    # ---------- 7. ポジションサイジング：ケリー基準と推定誤差 ----------
    print("## 7. ポジションサイジング：ケリー基準と推定誤差（シミュレーション）\n")
    mu_true, sig_ = 0.06, 0.18  # 想定：リスク資産の超過リターン年6%、ボラ18%（月次で正規分布）
    kelly = mu_true / sig_ ** 2
    print(f"想定：超過リターン年{mu_true*100:.0f}%、ボラ年{sig_*100:.0f}% → 理論上のフル・ケリー＝μ/σ² ＝ {kelly:.2f}倍（レバレッジ）\n")
    print("| 投資比率（自己資金に対する倍率） | 真の期待値が想定どおり：20年後 中央値 | 最大DD −50%超の確率 | 真の期待値が想定の半分：20年後 中央値 | 最大DD −50%超の確率 |\n|---|---|---|---|---|")
    n, T = 10000, 240
    for f in sorted([0.5, 1.0, kelly / 2, kelly, kelly * 1.5]):
        out = []
        for mu in [mu_true, mu_true / 2]:
            r = RNG.normal(mu / 12, sig_ / np.sqrt(12), size=(n, T))
            port = np.clip(f * r, -0.999, None)
            eq = np.cumprod(1 + port, axis=1)
            mdd = (eq / np.maximum.accumulate(eq, axis=1) - 1).min(axis=1)
            out += [np.median(eq[:, -1]), (mdd < -0.5).mean()]
        print(f"| {f:.2f}倍 | {out[0]:.2f} | {out[1]*100:.0f}% | {out[2]:.2f} | {out[3]*100:.0f}% |")
    print("\n（現金部分の金利、借入コスト、取引コストは0と仮定。月次の正規分布なので裾の厚さは含まない）\n")

    # ---------- 8. 損切りルール ----------
    print("## 8. 損切り（トレーリング・ストップ）の検証（日次、片道コスト0.1%、非保有時の金利0%）\n")
    print("ルール：直近の高値から X% 下落したら売り、終値が200日移動平均を上回ったら買い戻す。\n")
    print("| 指数 | ルール | 年率リターン | 最大DD | 売買回数/年 | 保有率 |\n|---|---|---|---|---|---|")
    for sid in ["NASDAQCOM", "NIKKEI225"]:
        c = pd.to_numeric(pd.read_csv(f"{pxdir}/{sid}.csv", index_col=0, parse_dates=True).iloc[:, 0], errors="coerce").dropna()
        r = c.pct_change().fillna(0).to_numpy()
        px, sma = c.to_numpy(), c.rolling(200).mean().to_numpy()
        yrs = len(c) / 252
        for X in [None, 0.10, 0.20, 0.30]:
            pos, peak, held, trades = 1, px[0], [], 0
            for i in range(len(px)):
                held.append(pos)
                if pos:
                    peak = max(peak, px[i])
                    if X is not None and px[i] <= peak * (1 - X):
                        pos, trades = 0, trades + 1
                else:
                    if not np.isnan(sma[i]) and px[i] > sma[i]:
                        pos, peak, trades = 1, px[i], trades + 1
            h = np.array(held, dtype=float)
            hp = np.r_[1, h[:-1]]  # 翌日から反映
            ret = hp * r - np.abs(np.diff(np.r_[1, hp])) * 0.001
            eq = np.cumprod(1 + ret)
            mdd = (eq / np.maximum.accumulate(eq) - 1).min()
            name = "買い持ち" if X is None else f"高値から−{int(X*100)}%で売り"
            print(f"| {sid} | {name} | {(eq[-1]**(1/yrs)-1)*100:.1f}% | {mdd*100:.1f}% | {trades/yrs:.2f} | {hp.mean()*100:.0f}% |")
    print()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
