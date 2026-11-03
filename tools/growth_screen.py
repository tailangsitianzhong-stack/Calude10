"""過去時点（2016年初め）の成長株スクリーニングを再現し、その後10年の結果を追跡する（第6段階の実践課題）。

使い方:
    python3 -I tools/growth_screen.py <SEC frames JSONのディレクトリ>

データ: SEC EDGAR XBRL frames API（https://data.sec.gov/api/xbrl/frames/...）
  us-gaap/Revenues, SalesRevenueNet, RevenueFromContractWithCustomerExcludingAssessedTax（CY2012, CY2015, CY2025）
  us-gaap/OperatingIncomeLoss（CY2015）、dei/EntityPublicFloat（CY2015Q2I, CY2025Q2I）

時点の扱い:
  - スクリーニングに使うのは2015年度までの売上・営業利益（2016年初めに10-Kで入手可能）。
  - frames は「最後に提出された値」を返すため、後の年度の10-Kで修正（再表示）された値が入りうる（軽微な先読み）。
  - 2015年の母集団には、その後に消滅した企業も含まれる（消滅前の最後の10-Kに2015年の値があるため）＝生存者バイアスを避けられる。
"""
import json
import sys

import numpy as np
import pandas as pd

REV_TAGS = ["Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerExcludingAssessedTax"]


def frame(base, name):
    try:
        d = json.load(open(f"{base}/{name}.json"))["data"]
    except (FileNotFoundError, KeyError, ValueError):
        return pd.Series(dtype=float), {}
    s = pd.Series({x["cik"]: x["val"] for x in d}, dtype=float)
    names = {x["cik"]: x["entityName"] for x in d}
    return s, names


def revenue(base, year):
    """複数の売上タグのうち最大値を採る（一部の売上項目だけのタグを拾う誤りを避けるため）。
    桁違いの誤値は、隣の年度との整合性チェック（main）で除外する。"""
    cols, names = {}, {}
    for t in REV_TAGS:
        s, n = frame(base, f"{t}_CY{year}")
        cols[t] = s
        names = {**n, **names}
    return pd.DataFrame(cols).max(axis=1), names


def md(df, pct=(), dec=2):
    cols = list(df.columns)
    lines = ["| " + " | ".join([df.index.name or ""] + cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for i, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, str):
                cells.append(v)
            elif pd.isna(v):
                cells.append("—")
            elif c in pct:
                cells.append(f"{v*100:.1f}%")
            elif float(v).is_integer() and abs(v) >= 1:
                cells.append(f"{int(v):,}")
            else:
                cells.append(f"{v:.{dec}f}")
        lines.append(f"| {i} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def summarize(g):
    alive = g["rev2025"].notna()
    fut = g.loc[alive, "fut_cagr"]
    fl = g["float_mult"].dropna()
    return pd.Series({
        "企業数": len(g),
        "2025年に売上の報告あり": alive.mean(),
        "将来10年の売上CAGR 中央値（報告ありのみ）": fut.median(),
        "売上CAGR≥15%を10年継続": (g["fut_cagr"] >= 0.15).sum() / len(g),
        "売上が減少": (g["fut_cagr"] < 0).sum() / len(g),
        "公開流通時価総額の倍率 中央値（2015→2025）": fl.median(),
        "同 倍率≥5倍の割合（データありのみ）": (fl >= 5).mean() if len(fl) else np.nan,
    })


def main(base):
    r12, _ = revenue(base, 2012)
    r15, names = revenue(base, 2015)
    r25, _ = revenue(base, 2025)
    r14, _ = revenue(base, 2014)
    r24, _ = revenue(base, 2024)
    op15, _ = frame(base, "OperatingIncomeLoss_CY2015")
    f15, _ = frame(base, "float_CY2015Q2I")
    f25, _ = frame(base, "float_CY2025Q2I")

    df = pd.DataFrame({"rev2012": r12, "rev2014": r14, "rev2015": r15, "rev2024": r24, "rev2025": r25, "op2015": op15,
                       "float2015": f15, "float2025": f25})
    df["name"] = pd.Series(names)
    u0 = df[(df["rev2015"] >= 1e8) & (df["rev2012"] > 1e6)]  # 2015年度の売上1億ドル以上
    # 整合性チェック：2014→2015 が0.5〜2倍、2012→2014 が0.1〜10倍でない企業はデータ誤りの疑いとして除外
    ok = (u0["rev2015"] / u0["rev2014"]).between(0.5, 2) & (u0["rev2014"] / u0["rev2012"]).between(0.1, 10)
    u = u0[ok].copy()
    excluded = len(u0) - len(u)
    # 2025年：2024→2025 が0.5〜2倍でなければ「不整合」として将来の値を使わない（消滅とは区別する）
    reported25 = u["rev2025"].notna()
    bad25 = reported25 & ~(u["rev2025"] / u["rev2024"]).between(0.5, 2)
    u.loc[bad25, "rev2025"] = np.nan
    u["past_cagr"] = (u["rev2015"] / u["rev2012"]) ** (1 / 3) - 1
    u["fut_cagr"] = (u["rev2025"] / u["rev2015"]) ** (1 / 10) - 1
    u["op_margin"] = u["op2015"] / u["rev2015"]
    u["float_mult"] = u["float2025"] / u["float2015"]
    u.loc[(u["float2015"] <= 0) | (u["float2025"] <= 0), "float_mult"] = np.nan

    print(f"## 母集団\n\n2015年度の売上が1億ドル以上、2012年度の売上が100万ドル超の企業{len(u0):,}社のうち、"
          f"2012・2014・2015年度の整合性チェックを通った **{len(u):,}社**（{excluded}社をデータ誤りの疑いで除外）。\n"
          f"2025年度に売上の報告があり2024年度と整合する企業：{u['rev2025'].notna().sum():,}社"
          f"（報告はあるが2024年度と不整合のため除外：{bad25.sum()}社）\n")

    # 1. 過去の成長率の5分位と将来
    u["q"] = pd.qcut(u["past_cagr"], 5, labels=["Q1（低成長）", "Q2", "Q3", "Q4", "Q5（高成長）"])
    t = u.groupby("q", observed=True).apply(summarize)
    rng = u.groupby("q", observed=True)["past_cagr"].agg(["min", "max"])
    t.insert(0, "過去3年の売上CAGR（範囲）", [f"{a*100:.0f}%〜{b*100:.0f}%" for a, b in zip(rng["min"], rng["max"])])
    t.index.name = "過去の成長率の5分位"
    print("## 1. 過去3年（2012→2015）の売上成長率別に見た、その後10年（2015→2025）\n")
    print(md(t, pct=["2025年に売上の報告あり", "将来10年の売上CAGR 中央値（報告ありのみ）", "売上CAGR≥15%を10年継続",
                     "売上が減少", "同 倍率≥5倍の割合（データありのみ）"]) + "\n")
    alive = u[u["rev2025"].notna()]
    rho = alive["past_cagr"].rank().corr(alive["fut_cagr"].rank())
    print(f"過去3年と将来10年の売上成長率の順位相関（2025年に報告ありの企業）：**{rho:.3f}**\n")

    # 2. スクリーニングの組み合わせ
    screens = {
        "全企業（基準率）": u,
        "A. 高成長（過去3年CAGR≥25%）": u[u["past_cagr"] >= 0.25],
        "B. 高成長＋高収益（A かつ営業利益率≥10%）": u[(u["past_cagr"] >= 0.25) & (u["op_margin"] >= 0.10)],
        "C. 高成長＋赤字（A かつ営業赤字）": u[(u["past_cagr"] >= 0.25) & (u["op_margin"] < 0)],
        "D. 安定成長＋高収益（CAGR 10〜25%、営業利益率≥15%）": u[u["past_cagr"].between(0.10, 0.25) & (u["op_margin"] >= 0.15)],
        "E. 低成長（過去3年CAGR<0%）": u[u["past_cagr"] < 0],
    }
    t = pd.DataFrame({k: summarize(v) for k, v in screens.items()}).T
    t.index.name = "スクリーニング（2016年初めの情報のみ）"
    print("## 2. スクリーニング条件別の10年後\n")
    print(md(t, pct=["2025年に売上の報告あり", "将来10年の売上CAGR 中央値（報告ありのみ）", "売上CAGR≥15%を10年継続",
                     "売上が減少", "同 倍率≥5倍の割合（データありのみ）"]) + "\n")

    # 3. 将来の勝者は2015年時点でどう見えていたか
    big = u[u["rev2015"] >= 5e8]
    win = big[big["fut_cagr"] >= 0.20].sort_values("fut_cagr", ascending=False)
    print(f"## 3. 将来の勝者（2015年度売上5億ドル以上で、2015→2025年の売上CAGR≥20%）：{len(win)}社 / {len(big)}社\n")
    print("2015年時点の特徴を、同じ規模の全企業と比較：\n")
    comp = pd.DataFrame({
        "将来の勝者": [win["past_cagr"].median(), win["op_margin"].median(), (win["past_cagr"] >= 0.25).mean()],
        "同規模の全企業": [big["past_cagr"].median(), big["op_margin"].median(), (big["past_cagr"] >= 0.25).mean()],
    }, index=["過去3年の売上CAGR 中央値", "2015年の営業利益率 中央値", "スクリーンA（過去CAGR≥25%）に入っていた割合"])
    comp.index.name = "2015年時点の指標"
    print(md(comp, pct=list(comp.columns)) + "\n")
    show = win.head(20)[["name", "rev2015", "past_cagr", "op_margin", "fut_cagr", "float_mult"]].copy()
    show["rev2015"] = (show["rev2015"] / 1e9).round(2)
    show.columns = ["企業名（SEC登録名）", "2015年売上（$B）", "過去3年CAGR", "2015年営業利益率", "将来10年CAGR", "流通時価総額の倍率"]
    show.index.name = "CIK"
    print("上位20社（売上成長率順。事後に判明した結果であり、推奨ではない）：\n")
    print(md(show, pct=["過去3年CAGR", "2015年営業利益率", "将来10年CAGR"]) + "\n")

    # 4. 2015年の高成長企業はその後どうなったか
    star = big[big["past_cagr"] >= 0.25].sort_values("past_cagr", ascending=False)
    show = star.head(20)[["name", "rev2015", "past_cagr", "op_margin", "fut_cagr", "float_mult"]].copy()
    show["rev2015"] = (show["rev2015"] / 1e9).round(2)
    show.columns = ["企業名（SEC登録名）", "2015年売上（$B）", "過去3年CAGR", "2015年営業利益率", "将来10年CAGR", "流通時価総額の倍率"]
    show.index.name = "CIK"
    print(f"## 4. 2015年時点の高成長企業（売上5億ドル以上、過去3年CAGR≥25%）：{len(star)}社の上位20社のその後\n")
    print("「将来10年CAGR」が空欄＝2025年に売上の報告なし（買収・非上場化・破綻・決算期やタグの変更を含む）。\n")
    print(md(show, pct=["過去3年CAGR", "2015年営業利益率", "将来10年CAGR"]) + "\n")
    s = summarize(star)
    print(f"この{len(star)}社全体：2025年に報告あり {s['2025年に売上の報告あり']*100:.0f}%、"
          f"将来10年CAGR中央値 {s['将来10年の売上CAGR 中央値（報告ありのみ）']*100:.1f}%、"
          f"CAGR≥15%を継続 {s['売上CAGR≥15%を10年継続']*100:.0f}%\n")

    # 5. データ品質
    acc_year = {}
    for t_ in REV_TAGS:
        try:
            for x in json.load(open(f"{base}/{t_}_CY2015.json"))["data"]:
                acc_year.setdefault(x["cik"], int("20" + x["accn"].split("-")[1]))
        except (FileNotFoundError, ValueError):
            pass
    ay = pd.Series(acc_year).reindex(u.index)
    print("## 5. データ品質の確認\n")
    print(f"- 2015年度の売上の値の出典（提出年）：2016年 {(ay==2016).mean()*100:.0f}%、2017年 {(ay==2017).mean()*100:.0f}%、"
          f"2018年以降 {(ay>=2018).mean()*100:.0f}%（2017年以降の値は、後の10-Kの比較年度の数値＝再表示を含みうる）\n")
    print(f"- 公開流通時価総額が両年ともある企業：{u['float_mult'].notna().sum():,}社 / {len(u):,}社\n")


if __name__ == "__main__":
    main(sys.argv[1])
