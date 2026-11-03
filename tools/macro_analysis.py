"""マクロ環境と業種別リターンの関係を検証する（第4段階の実践課題）。

使い方:
    python3 -I tools/macro_analysis.py <マクロデータのディレクトリ> <日経平均CSVのディレクトリ>

必要なデータ:
  - Kenneth French Data Library: 12_Industry_Portfolios.csv, F-F_Research_Data_Factors.csv（<dir>/ff/）
  - FRED: FEDFUNDS, GS10, TB3MS, CPIAUCSL, UNRATE, USREC, T10Y3M, DEXJPUS, BAA10YM（<dir>/）
  - FRED: NIKKEI225（第3段階と同じ）

先読みを防ぐため、局面の判定には月tのリターンに対して月t-1までに公表済みのデータを使う
（CPIは公表が翌月中旬なので2か月ラグ）。ただし景気後退（USREC）はNBERが事後に認定するため、
リアルタイムには分からない「説明用」の分類として扱う。
"""
import io
import sys

import numpy as np
import pandas as pd

IND_JA = {"NoDur": "非耐久消費財", "Durbl": "耐久消費財", "Manuf": "製造業", "Enrgy": "エネルギー",
          "Chems": "化学", "BusEq": "IT・事業設備", "Telcm": "通信", "Utils": "公益",
          "Shops": "小売・卸", "Hlth": "ヘルスケア", "Money": "金融", "Other": "その他"}


def french_block(path, header_marker):
    """French CSVのうち、指定見出しの直後の月次ブロック（YYYYMM行）を読む。"""
    lines = open(path, encoding="latin-1").read().splitlines()
    i = next(k for k, l in enumerate(lines) if header_marker in l)
    # 見出しの次がカラム行、その後に YYYYMM 行が続く
    j = i
    while not lines[j].strip().startswith(","):
        j += 1
    rows = [lines[j]]
    for l in lines[j + 1:]:
        key = l.split(",")[0].strip()
        if not (key.isdigit() and len(key) == 6):
            break
        rows.append(l)
    df = pd.read_csv(io.StringIO("\n".join(rows)), index_col=0)
    df.index = pd.to_datetime(df.index.astype(str).str.strip(), format="%Y%m").to_period("M")
    df.columns = [c.strip() for c in df.columns]
    df = df.apply(pd.to_numeric, errors="coerce")
    return df.replace([-99.99, -999], np.nan) / 100


def fred(base, sid, monthly=True):
    s = pd.read_csv(f"{base}/{sid}.csv", index_col=0, parse_dates=True).iloc[:, 0]
    s = pd.to_numeric(s, errors="coerce").dropna()
    if monthly:
        s.index = s.index.to_period("M")
        s = s.groupby(level=0).last()
    return s


def tstat(x):
    x = x.dropna()
    return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 else np.nan


def md(df, pct_cols=(), dec=2):
    cols = list(df.columns)
    out = ["| " + " | ".join([df.index.name or ""] + cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for i, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, str):
                cells.append(v)
            elif pd.isna(v):
                cells.append("—")
            elif c in pct_cols:
                cells.append(f"{v*100:.1f}%")
            else:
                cells.append(f"{v:.{dec}f}")
        out.append(f"| {i} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def main(base, pxdir):
    ind = french_block(f"{base}/ff/12_Industry_Portfolios.csv", "Average Value Weighted Returns -- Monthly")
    fac = french_block(f"{base}/ff/F-F_Research_Data_Factors.csv", "Mkt-RF")
    rf, mkt = fac["RF"], fac["Mkt-RF"]
    ex = ind.sub(rf, axis=0)
    ex["市場全体"] = mkt
    ex = ex.rename(columns=IND_JA)

    ff_ = fred(base, "FEDFUNDS")
    cpi = fred(base, "CPIAUCSL")
    rec = fred(base, "USREC")
    gs10, tb3 = fred(base, "GS10"), fred(base, "TB3MS")

    # ---------- 1. 金融政策・インフレ・景気局面別の業種超過リターン ----------
    d6 = (ff_ - ff_.shift(6)).shift(1)  # 月t-1時点で分かっている過去6か月のFF金利変化
    policy = pd.Series(np.where(d6 > 0.5, "引き締め", np.where(d6 < -0.5, "緩和", "中立")), index=d6.index)
    policy[d6.isna()] = np.nan
    infl = (cpi / cpi.shift(12) - 1).shift(2)
    infl_reg = pd.Series(np.where(infl >= 0.04, "高インフレ(≧4%)", "低インフレ(<4%)"), index=infl.index)
    infl_reg[infl.isna()] = np.nan
    rec_reg = rec.map({1: "景気後退(NBER)", 0: "景気拡大"})

    start = "1955-01"
    E = ex.loc[start:]
    print(f"## 1. 局面別の業種超過リターン（年率、{E.index[0]}〜{E.index[-1]}、時価加重、対T-Bill）\n")
    for name, reg in [("金融政策（過去6か月のFF金利変化 ±0.5%pt）", policy), ("インフレ（CPI前年比、2か月ラグ）", infl_reg),
                      ("景気（NBER認定、事後判定）", rec_reg)]:
        r = reg.reindex(E.index)
        tab = (E.groupby(r).mean() * 12).T
        tab.index.name = "業種"
        print(f"### {name}\n")
        print(md(tab, pct_cols=list(tab.columns)))
        print("\n月数: " + ", ".join(f"{k}={int(v)}" for k, v in r.value_counts().items()) + "\n")

    # 引き締め期と緩和期の差のt値（市場全体と主要業種）
    r = policy.reindex(E.index)
    rows = {}
    for c in E.columns:
        a, b = E[c][r == "引き締め"], E[c][r == "緩和"]
        diff = a.mean() - b.mean()
        se = np.sqrt(a.var() / len(a) + b.var() / len(b))
        rows[c] = {"引き締め−緩和(年率)": diff * 12, "t値": diff / se}
    t = pd.DataFrame(rows).T
    ri = infl_reg.reindex(E.index)
    for c in E.columns:
        a, b = E[c][ri == "高インフレ(≧4%)"], E[c][ri == "低インフレ(<4%)"]
        diff = a.mean() - b.mean()
        t.loc[c, "高インフレ−低インフレ(年率)"] = diff * 12
        t.loc[c, "t値(インフレ)"] = diff / np.sqrt(a.var() / len(a) + b.var() / len(b))
    t.index.name = "業種"
    print("### 局面間の差の検定（月次リターンの平均の差、独立性を仮定した単純なt値）\n")
    print(md(t, pct_cols=["引き締め−緩和(年率)", "高インフレ−低インフレ(年率)"]) + "\n")

    # ---------- 2. 金利感応度（市場要因を除いた10年金利変化へのベータ） ----------
    dy = (gs10 - gs10.shift(1)) / 100  # 小数（1%pt=0.01）
    print("## 2. 業種別の金利感応度：業種超過リターン = a + b×市場超過リターン + c×10年金利の変化\n")
    print("c は「市場全体の動きを除いた後、10年金利が1%pt上昇した月に、その業種が市場比で何%動くか」。\n")
    out = {}
    for label, a, b in [("1960〜1999", "1960-01", "1999-12"), ("2000〜2026", "2000-01", None)]:
        for c in IND_JA.values():
            y = ex[c].loc[a:b]
            X = pd.concat([mkt, dy], axis=1).reindex(y.index)
            X.columns = ["mkt", "dy"]
            m = pd.concat([y, X], axis=1).dropna()
            A = np.column_stack([np.ones(len(m)), m["mkt"], m["dy"]])
            coef, res, *_ = np.linalg.lstsq(A, m.iloc[:, 0], rcond=None)
            resid = m.iloc[:, 0] - A @ coef
            s2 = resid.var(ddof=3)
            se = np.sqrt(np.diag(s2 * np.linalg.inv(A.T @ A)))
            out.setdefault(c, {})[f"{label} 市場β"] = coef[1]
            out[c][f"{label} 金利c"] = coef[2]
            out[c][f"{label} t値"] = coef[2] / se[2]
    tab = pd.DataFrame(out).T
    tab.index.name = "業種"
    print(md(tab) + "\n")

    # ---------- 3. 逆イールドと景気後退 ----------
    spread = (gs10 - tb3).dropna()
    rec_m = rec.reindex(spread.index)
    rec_start = rec_m[(rec_m == 1) & (rec_m.shift(1) == 0)].index
    inv = spread < 0
    starts = inv[(inv) & (~inv.shift(1, fill_value=False)) & (~inv.shift(2, fill_value=False)) & (~inv.shift(3, fill_value=False))].index
    mk = (1 + mkt + rf)  # 市場の総リターン
    rows = []
    for s in starts:
        nxt = [r for r in rec_start if r > s]
        lead = (nxt[0] - s).n if nxt else None
        hit = lead is not None and lead <= 24
        fwd = mk.loc[s + 1: s + 12].prod() - 1 if s + 12 <= mk.index[-1] else np.nan
        fwd24 = mk.loc[s + 1: s + 24].prod() - 1 if s + 24 <= mk.index[-1] else np.nan
        rows.append({"逆転開始": str(s), "景気後退までの月数": str(lead) if lead is not None else "未発生",
                     "24か月以内に後退": "○" if hit else "×", "その後12か月の株式リターン": fwd, "その後24か月": fwd24})
    t = pd.DataFrame(rows).set_index("逆転開始")
    print("## 3. 逆イールド（10年国債−3か月T-Bill<0、月次平均）と景気後退\n")
    print(md(t, pct_cols=["その後12か月の株式リターン", "その後24か月"]) + "\n")
    missed = [str(r) for r in rec_start if r >= spread.index[0] and not any(0 < (r - s).n <= 24 for s in starts)]
    print(f"逆イールドが先行しなかった景気後退の開始月: {', '.join(missed) if missed else 'なし'}\n")
    print(f"全期間の株式（総リターン）の12か月リターンの平均: {(mk.rolling(12).apply(np.prod, raw=True) - 1).mean()*100:.1f}%\n")

    # ---------- 4. 株価と金利の相関（符号の変化） ----------
    c = pd.concat([mkt, dy], axis=1).dropna()
    c.columns = ["mkt", "dy"]
    dec = c.groupby((c.index.year // 10) * 10).apply(lambda x: x["mkt"].corr(x["dy"]))
    infl_avg = (cpi / cpi.shift(12) - 1).groupby((cpi.index.year // 10) * 10).mean()
    t = pd.DataFrame({"株式リターンと10年金利変化の相関": dec, "平均インフレ率": infl_avg.reindex(dec.index)})
    t.index = [f"{y}年代" for y in t.index]
    t.index.name = "期間"
    print("## 4. 株式リターンと10年金利変化の相関（月次）\n")
    print("正＝金利上昇月に株も上昇（株と債券価格は逆相関）、負＝金利上昇月に株が下落（株と債券が同方向）。\n")
    print(md(t, pct_cols=["平均インフレ率"]) + "\n")

    # ---------- 5. 日経平均とドル円 ----------
    nk = fred(pxdir, "NIKKEI225")
    fx = fred(base, "DEXJPUS")
    m = pd.concat([nk.pct_change(), fx.pct_change()], axis=1).dropna()
    m.columns = ["nk", "fx"]
    per = m.groupby((m.index.year // 10) * 10).apply(lambda x: pd.Series({"相関": x["nk"].corr(x["fx"]), "月数": str(len(x))}))
    per.index = [f"{y}年代" for y in per.index]
    per.index.name = "期間"
    print("## 5. 日経平均の月次リターンとドル円の変化率（正＝円安ドル高）の相関\n")
    print(md(per, dec=2) + "\n")

    # ---------- 6. 現在のマクロ指標（最新値） ----------
    latest = {
        "FF金利（月平均）": ff_, "10年国債利回り（月平均）": gs10, "3か月T-Bill（月平均）": tb3,
        "失業率": fred(base, "UNRATE"), "Baa社債−10年国債スプレッド": fred(base, "BAA10YM"),
    }
    print("## 6. 最新値（FRED、取得時点）\n")
    print("| 指標 | 時点 | 値 | 1年前 |\n|---|---|---|---|")
    for k, s in latest.items():
        print(f"| {k} | {s.index[-1]} | {s.iloc[-1]:.2f}% | {s.iloc[-13]:.2f}% |")
    yoy = cpi / cpi.shift(12) - 1
    print(f"| CPI前年比 | {yoy.index[-1]} | {yoy.iloc[-1]*100:.2f}% | {yoy.iloc[-13]*100:.2f}% |")
    t10 = fred(base, "T10Y3M", monthly=False)
    print(f"| 10年−3か月スプレッド（日次） | {t10.index[-1].date()} | {t10.iloc[-1]:.2f}%pt | — |")
    print(f"| ドル円 | {fx.index[-1]} | {fx.iloc[-1]:.2f} | {fx.iloc[-13]:.2f} |")
    for k, sid in [("日本 長期金利（10年、OECD）", "IRLTLT01JPM156N"), ("日本 コールレート（OECD）", "IRSTCI01JPM156N")]:
        x = fred(base, sid)
        print(f"| {k} | {x.index[-1]} | {x.iloc[-1]:.2f}% | {x.iloc[-13]:.2f}% |")
    for k, sid, unit in [("ECB 預金ファシリティ金利", "ECBDFR", "%"), ("WTI原油（ドル/バレル）", "DCOILWTICO", "")]:
        x = fred(base, sid, monthly=False)
        ago = x[:x.index[-1] - pd.DateOffset(years=1)]
        print(f"| {k} | {x.index[-1].date()} | {x.iloc[-1]:.2f}{unit} | {ago.iloc[-1]:.2f}{unit} |")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
