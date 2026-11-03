"""投資戦略（ファクター）の長期検証とバークシャー・ハサウェイの成績分析（第5段階の実践課題）。

使い方:
    python3 -I tools/strategy_analysis.py <French CSVのディレクトリ> <バークシャー2025年書簡のテキスト> <12業種CSVのパス>

データ:
  - Kenneth French Data Library（米国: 5ファクター, Mom, ST_Rev, LT_Rev, D/P・BE/ME・ME・OP・VAR別ポートフォリオ。
    日本: Japan 5 Factors, Japan MOM）
  - Berkshire Hathaway 2025 Annual Letter（pdftotext -layout で変換したテキスト）
"""
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from macro_analysis import french_block, md  # noqa: E402


def stats(r, rf=None):
    """月次リターン系列の要約。rf を与えると r は総リターンとみなしシャープを超過で計算。"""
    r = r.dropna()
    ex = r - rf.reindex(r.index) if rf is not None else r
    eq = (1 + r).cumprod()
    roll10 = (1 + r).rolling(120).apply(np.prod, raw=True) ** (1 / 10) - 1
    return {
        "期間": f"{r.index[0]}〜{r.index[-1]}",
        "年率リターン(幾何)": eq.iloc[-1] ** (12 / len(r)) - 1,
        "ボラ": r.std() * np.sqrt(12),
        "シャープ": ex.mean() / ex.std() * np.sqrt(12),
        "t値": ex.mean() / ex.std() * np.sqrt(len(ex)),
        "最大DD": (eq / eq.cummax() - 1).min(),
        "最悪の10年(年率)": roll10.min(),
    }


def factor_section(base):
    f5 = french_block(f"{base}/F-F_Research_Data_5_Factors_2x3.csv", "Mkt-RF")
    mom = french_block(f"{base}/F-F_Momentum_Factor.csv", ",Mom")
    st = french_block(f"{base}/F-F_ST_Reversal_Factor.csv", ",ST_Rev")
    lt = french_block(f"{base}/F-F_LT_Reversal_Factor.csv", ",LT_Rev")
    F = pd.concat([f5.drop(columns="RF"), mom, st, lt], axis=1)
    names = {"Mkt-RF": "市場（株式リスクプレミアム）", "SMB": "小型株（SMB）", "HML": "バリュー（HML）",
             "RMW": "収益性・クオリティ（RMW）", "CMA": "保守的投資（CMA）", "Mom": "モメンタム（Mom）",
             "ST_Rev": "短期リバーサル（1か月逆張り）", "LT_Rev": "長期リバーサル（3〜5年逆張り）"}
    print("## 1. 米国のファクター（ロング・ショート、月次）の長期成績\n")
    print("各ファクターは「割安−割高」「小型−大型」などの差のポートフォリオ。市場はT-Billに対する超過リターン。\n")
    for label, a, b in [("1963-07〜2026（5ファクターが揃う期間）", "1963-07", None),
                        ("1963-07〜1999", "1963-07", "1999-12"), ("2000〜2026", "2000-01", None)]:
        rows = {names[c]: stats(F[c].loc[a:b]) for c in F.columns}
        t = pd.DataFrame(rows).T.drop(columns="期間")
        t.index.name = "ファクター"
        print(f"### {label}\n")
        print(md(t, pct_cols=["年率リターン(幾何)", "ボラ", "最大DD", "最悪の10年(年率)"]) + "\n")
    # 発表前後（McLean & Pontiff 2016 の検証の簡易版）
    pub = {"HML": ("1992-06", "Fama & French (1992)"), "SMB": ("1981-03", "Banz (1981)"),
           "Mom": ("1993-03", "Jegadeesh & Titman (1993)"), "RMW": ("2013-04", "Novy-Marx (2013)")}
    rows = {}
    for c, (d, paper) in pub.items():
        s = F[c].dropna()
        pre, post = s[:d], s[d:]
        rows[names[c]] = {"論文": paper, "発表前 年率(算術)": pre.mean() * 12, "発表前 t値": pre.mean() / pre.std() * np.sqrt(len(pre)),
                          "発表後 年率(算術)": post.mean() * 12, "発表後 t値": post.mean() / post.std() * np.sqrt(len(post)),
                          "変化率": post.mean() / pre.mean() - 1}
    t = pd.DataFrame(rows).T
    t.index.name = "ファクター"
    print("### 学術論文の発表前後の比較\n")
    print(md(t, pct_cols=["発表前 年率(算術)", "発表後 年率(算術)", "変化率"]) + "\n")
    return f5["RF"], f5["Mkt-RF"] + f5["RF"]


def portfolio_section(base, rf3, mkt3):
    print("## 2. 投資手法の代理ポートフォリオ（時価加重、ロングのみ、総リターン）\n")
    print("市場＝CRSP全銘柄の時価加重。各手法は、その指標の上位・下位10%（または該当グループ）の銘柄群。\n")
    spec = [
        ("Portfolios_Formed_on_D-P.csv", "Value Weight Returns -- Monthly", [("高配当（配当利回り上位10%）", "Hi 10"), ("無配", "<= 0")]),
        ("Portfolios_Formed_on_BE-ME.csv", "Value Weight Returns -- Monthly", [("バリュー（簿価時価比率 上位10%）", "Hi 10"), ("グロース（簿価時価比率 下位10%）", "Lo 10")]),
        ("Portfolios_Formed_on_ME.csv", "Value Weight Returns -- Monthly", [("小型株（時価総額 下位10%）", "Lo 10"), ("大型株（時価総額 上位10%）", "Hi 10")]),
        ("Portfolios_Formed_on_OP.csv", "Value Weight Returns -- Monthly", [("クオリティ（営業利益率 上位10%）", "Hi 10"), ("低収益性（営業利益率 下位10%）", "Lo 10")]),
        ("Portfolios_Formed_on_VAR.csv", "Value Weighted Returns -- Monthly", [("低ボラティリティ（下位10%）", "Lo 10"), ("高ボラティリティ（上位10%）", "Hi 10")]),
    ]
    P = {"市場全体（インデックス）": mkt3}
    for f, hdr, cols in spec:
        df = french_block(f"{base}/{f}", hdr)
        for name, c in cols:
            P[name] = df[c]
    P = pd.DataFrame(P)
    for label, a, b in [("1963-07〜2026", "1963-07", None), ("2000〜2026", "2000-01", None)]:
        rows = {k: stats(P[k].loc[a:b], rf3) for k in P.columns}
        t = pd.DataFrame(rows).T.drop(columns=["期間", "t値"])
        t.index.name = "手法（代理）"
        print(f"### {label}\n")
        print(md(t, pct_cols=["年率リターン(幾何)", "ボラ", "最大DD", "最悪の10年(年率)"]) + "\n")
    return P


def theme_proxy(ind_path, rf3, mkt3):
    """テーマ株投資の簡易な代理：過去12か月（直近1か月を除く）に最も上昇した3業種を毎月保有。"""
    ind = french_block(ind_path, "Average Value Weighted Returns -- Monthly")
    past = (1 + ind).rolling(11).apply(np.prod, raw=True).shift(2) - 1  # t-12〜t-2
    rank = past.rank(axis=1, ascending=False)
    top = (rank <= 3).astype(float)
    bot = (rank >= 10).astype(float)
    r_top = (ind * top).sum(axis=1) / top.sum(axis=1)
    r_bot = (ind * bot).sum(axis=1) / bot.sum(axis=1)
    print("## 3. 人気業種への追随（業種モメンタム）：テーマ株投資の簡易な代理\n")
    print("毎月、過去12か月（直近1か月を除く）の上昇率が上位3業種を等金額で保有（12業種、売買コスト未考慮）。\n")
    rows = {}
    for label, a, b in [("1963-07〜2026", "1963-07", None), ("2000〜2026", "2000-01", None)]:
        rows[f"上位3業種 {label}"] = stats(r_top.loc[a:b], rf3)
        rows[f"下位3業種 {label}"] = stats(r_bot.loc[a:b], rf3)
        rows[f"市場全体 {label}"] = stats(mkt3.loc[a:b], rf3)
    t = pd.DataFrame(rows).T.drop(columns=["期間", "t値"])
    t.index.name = "戦略"
    print(md(t, pct_cols=["年率リターン(幾何)", "ボラ", "最大DD", "最悪の10年(年率)"]) + "\n")


def japan_section(base):
    j5 = french_block(f"{base}/Japan_5_Factors.csv", "Mkt-RF")
    jm = french_block(f"{base}/Japan_MOM_Factor.csv", ",WML")
    J = pd.concat([j5.drop(columns="RF"), jm], axis=1)
    names = {"Mkt-RF": "市場（米ドル建て、対T-Bill）", "SMB": "小型株", "HML": "バリュー", "RMW": "収益性・クオリティ",
             "CMA": "保守的投資", "WML": "モメンタム"}
    print("## 4. 日本のファクター（French、Bloombergデータ、米ドル建て）\n")
    for label, a, b in [("全期間", None, None), ("1990-07〜2007", None, "2007-12"), ("2008〜2026", "2008-01", None)]:
        rows = {names[c]: stats(J[c].loc[a:b]) for c in J.columns}
        t = pd.DataFrame(rows).T
        t.index.name = "ファクター"
        print(f"### {label}\n")
        print(md(t, pct_cols=["年率リターン(幾何)", "ボラ", "最大DD", "最悪の10年(年率)"]) + "\n")


def berkshire_section(txt):
    rows = []
    for line in open(txt, encoding="utf-8"):
        m = re.match(r"^(19[6-9]\d|20[0-2]\d)\s[ .]+\s+(\(?-?[\d.]+\)?)%?\s+(\(?-?[\d.]+\)?)%?\s*$", line)
        if m:
            conv = lambda s: -float(s.strip("()")) if s.startswith("(") else float(s)
            rows.append((int(m.group(1)), conv(m.group(2)) / 100, conv(m.group(3)) / 100))
    b = pd.DataFrame(rows, columns=["年", "BRK", "SP"]).drop_duplicates("年").set_index("年")
    print("## 5. バークシャー・ハサウェイ：1株当たり市場価値の変化 vs S&P500（配当込み）\n")
    print(f"出所：2025年 株主への手紙の成績表（{b.index[0]}〜{b.index[-1]}年、{len(b)}年分）。\n")
    cagr = lambda x: (1 + x).prod() ** (1 / len(x)) - 1
    out = {}
    for a, z in [(1965, 1974), (1975, 1984), (1985, 1994), (1995, 2004), (2005, 2014), (2015, 2025), (1965, 2025)]:
        x = b.loc[a:z]
        out[f"{a}〜{z}"] = {"BRK 年率": cagr(x["BRK"]), "S&P500 年率": cagr(x["SP"]), "差": cagr(x["BRK"]) - cagr(x["SP"]),
                           "S&Pを下回った年": f"{(x['BRK'] < x['SP']).sum()}/{len(x)}", "BRK 最悪の年": x["BRK"].min()}
    t = pd.DataFrame(out).T
    t.index.name = "期間"
    print(md(t, pct_cols=["BRK 年率", "S&P500 年率", "差", "BRK 最悪の年"]) + "\n")
    eq = (1 + b["BRK"]).cumprod()
    print(f"検算：1965〜2025年の年率 BRK {cagr(b['BRK'])*100:.1f}%、S&P500 {cagr(b['SP'])*100:.1f}%"
          f"（書簡の記載は 19.7% と 10.5%）。年次ベースの最大下落（年末値）：{(eq/eq.cummax()-1).min()*100:.1f}%\n")


if __name__ == "__main__":
    base, brk, ind = sys.argv[1], sys.argv[2], sys.argv[3]
    rf3, mkt3 = factor_section(base)
    portfolio_section(base, rf3, mkt3)
    theme_proxy(ind, rf3, mkt3)
    japan_section(base)
    berkshire_section(brk)
