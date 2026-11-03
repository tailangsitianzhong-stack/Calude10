"""企業価値評価（第7段階の実践課題）：TTM財務 → 資本コスト → DCF（3シナリオ）→ 感応度 → 逆DCF → マルチプル。

使い方:
    python3 -I tools/valuation.py <companyfacts JSONのディレクトリ>

前提（すべてこのファイル冒頭の ASSUMPTIONS で明示。変更すれば結果が変わる）:
  - 株価：2026-11-03 終値（stockanalysis.com の履歴表、二次情報）
  - リスクフリー金利・株式リスクプレミアム：Damodaran（2026-11-01 時点）
  - アンレバード・ベータ：Damodaran 業種別（2026年1月）
数値はすべて米ドル。売上・利益・CFは直近4四半期（TTM）= 直近10-K + 当年YTD − 前年同期YTD。
"""
import json
import sys
from datetime import date

import numpy as np

ASSUMPTIONS = {
    "date": "2026-11-03",
    "rf": 0.0507,     # 米10年国債 5.29% − デフォルト・スプレッド 0.22%（Damodaran の調整に合わせる）
    "erp": 0.0463,    # 4.41%（TTM、調整後配当性向）+ 0.22%（同上）
    "tax": 0.21,      # 米国の法定税率（限界税率）
    "debt_spread": 0.009,  # 高格付け（A格前後）事業会社の社債スプレッドの仮定
    "probs": {"弱気": 0.25, "中立": 0.50, "強気": 0.25},
    "companies": {
        "ko": {"name": "Coca-Cola", "price": 94.80, "beta_u": 0.58,  # 清涼飲料（現金補正後）
               # 第2段階で計算した FY2021〜2025 の FCF（$B）と一時的支出の足し戻し（2024 IRS供託金6.0、2025 fairlife 6.1）
               "fcf_hist": [11.26, 9.53, 9.75, 4.74, 5.30], "fcf_adj": [0, 0, 0, 6.0, 6.1],
               "scen": {"弱気": (0.02, 0.020, +0.005), "中立": (0.05, 0.025, 0.0), "強気": (0.07, 0.030, -0.005)}},
        "pep": {"name": "PepsiCo", "price": 137.27, "beta_u": 0.53,  # 清涼飲料0.58と食品加工0.47の平均（事業構成の近似）
                "fcf_hist": [6.99, 5.60, 7.92, 7.19, 7.67], "fcf_adj": [0, 0, 0, 0, 0],
                "scen": {"弱気": (0.01, 0.020, +0.005), "中立": (0.04, 0.025, 0.0), "強気": (0.06, 0.030, -0.005)}},
    },
}
# scen = (1〜5年目のFCFF成長率, 永久成長率, WACCへの加算)。6〜10年目は永久成長率へ直線的に収れん。

FLOW = {
    "rev": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"],
    "op": ["OperatingIncomeLoss"], "ni": ["NetIncomeLoss"], "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"],
    "tax": ["IncomeTaxExpenseBenefit"], "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "da": ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndOther"],
    "div": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"], "int": ["InterestExpense", "InterestExpenseNonoperating"],
    "eps": ["EarningsPerShareDiluted"],
}
STOCK = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue"], "sti": ["ShortTermInvestments", "MarketableSecuritiesCurrent"],
    "ltd_nc": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "debt_cur": ["DebtCurrent"], "ltd_cur": ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "st_debt": ["NotesAndLoansPayable", "ShortTermBorrowings", "CommercialPaper"],
    "equity": ["StockholdersEquity"], "nci": ["MinorityInterest"],
}


def dur(v):
    return (date.fromisoformat(v["end"]) - date.fromisoformat(v["start"])).days


def vals(g, tags):
    for t in tags:
        if t in g:
            u = g[t]["units"]
            return u.get("USD") or u.get("USD/shares") or []
    return []


def ttm(g, tags, q_accn):
    v = vals(g, tags)
    fy = [x for x in v if x.get("form", "").startswith("10-K") and "start" in x and 350 <= dur(x) <= 380]
    if not fy:
        return None, None
    fy_last = max(fy, key=lambda x: (x["end"], x["filed"]))
    ytd = [x for x in v if x.get("accn") == q_accn and "start" in x and dur(x) > 200]
    if len(ytd) < 2:
        return None, None
    ytd.sort(key=lambda x: x["end"])
    cur, prev = ytd[-1], ytd[0]
    return fy_last["val"] + cur["val"] - prev["val"], f'{fy_last["end"]}年度 + {cur["start"]}〜{cur["end"]} − 前年同期'


def latest_stock(g, tags, q_accn):
    for t in tags:
        v = [x for x in vals(g, [t]) if x.get("accn") == q_accn and "start" not in x]
        if v:
            return max(v, key=lambda x: x["end"])["val"]
    return None


def load(path):
    f = json.load(open(path))["facts"]
    g = f["us-gaap"]
    q = [x for x in g["NetIncomeLoss"]["units"]["USD"] if x["form"] == "10-Q"]
    q_accn = max(q, key=lambda x: x["filed"])["accn"]
    d, note = {}, None
    for k, t in FLOW.items():
        d[k], n = ttm(g, t, q_accn)
        note = note or n
    for k, t in STOCK.items():
        d[k] = latest_stock(g, t, q_accn)
    sh = f["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
    d["shares"] = max(sh, key=lambda x: x["end"])["val"]
    cur = d["debt_cur"] if d["debt_cur"] is not None else (d["ltd_cur"] or 0) + (d["st_debt"] or 0)
    d["debt"] = (d["ltd_nc"] or 0) + cur
    d["cash_all"] = (d["cash"] or 0) + (d["sti"] or 0)
    d["note"], d["q_accn"] = note, q_accn
    return d


def dcf(fcff0, wacc, g1, gT, years=10):
    """1〜5年目は g1、6〜10年目は gT へ直線的に収れん。終価はゴードン式。"""
    pv, f = 0.0, fcff0
    for t in range(1, years + 1):
        g = g1 if t <= 5 else g1 + (gT - g1) * (t - 5) / (years - 5)
        f *= 1 + g
        pv += f / (1 + wacc) ** t
    tv = f * (1 + gT) / (wacc - gT)
    pv_tv = tv / (1 + wacc) ** years
    return pv + pv_tv, pv_tv / (pv + pv_tv)


def solve_g1(fcff0, wacc, gT, target):
    lo, hi = -0.10, 0.40
    for _ in range(100):
        mid = (lo + hi) / 2
        if dcf(fcff0, wacc, mid, gT)[0] < target:
            lo = mid
        else:
            hi = mid
    return mid


def pct(x):
    return f"{x*100:.1f}%"


def main(base):
    A = ASSUMPTIONS
    print(f"# 評価の前提（{A['date']}）\n")
    print(f"- リスクフリー金利 {pct(A['rf'])}、株式リスクプレミアム {pct(A['erp'])}、税率 {pct(A['tax'])}、社債スプレッド {pct(A['debt_spread'])}")
    print(f"- シナリオの確率：" + "、".join(f"{k} {int(v*100)}%" for k, v in A["probs"].items()) + "\n")
    for tk, c in A["companies"].items():
        d = load(f"{base}/{tk}.json")
        P, sh = c["price"], d["shares"]
        E = P * sh
        D, cash = d["debt"], d["cash_all"]
        ev = E + D - cash + (d["nci"] or 0)
        t_eff = d["tax"] / d["pretax"]
        ebitda = d["op"] + d["da"]
        fcf = d["cfo"] - d["capex"]
        fcff = fcf + d["int"] * (1 - A["tax"])
        beta_l = c["beta_u"] * (1 + (1 - A["tax"]) * D / E)
        ke = A["rf"] + beta_l * A["erp"]
        kd = A["rf"] + A["debt_spread"]
        wacc = (E * ke + D * kd * (1 - A["tax"])) / (E + D)
        eps = d["ni"] / (d["ni"] / d["eps"]) if d["eps"] else None
        print(f"\n## {c['name']}（{tk.upper()}）\n")
        print(f"TTMの算出：{d['note']}（最新10-Q 受付番号 {d['q_accn']}）\n")
        print("### 1. 現在の評価（TTM）\n")
        print("| 項目 | 値 |\n|---|---|")
        rows = [
            ("株価（終値）", f"${P:.2f}"), ("発行済株式数", f"{sh/1e9:.3f}十億株"), ("時価総額", f"${E/1e9:.1f}B"),
            ("有利子負債 / 現金・短期投資", f"${D/1e9:.1f}B / ${cash/1e9:.1f}B"), ("企業価値（EV）", f"${ev/1e9:.1f}B"),
            ("売上高（TTM）", f"${d['rev']/1e9:.2f}B"), ("営業利益（TTM）／利益率", f"${d['op']/1e9:.2f}B／{pct(d['op']/d['rev'])}"),
            ("純利益（TTM）", f"${d['ni']/1e9:.2f}B"), ("希薄化EPS（TTM）", f"${d['eps']:.2f}"),
            ("PER（株価÷TTM EPS）", f"{P/d['eps']:.1f}倍"), ("益回り（EPS÷株価）", pct(d['eps']/P)),
            ("EV/EBITDA", f"{ev/ebitda:.1f}倍"), ("EV/売上", f"{ev/d['rev']:.2f}倍"),
            ("FCF（営業CF−設備投資、TTM）", f"${fcf/1e9:.2f}B"), ("FCF利回り（FCF÷時価総額）", pct(fcf / E)),
            ("配当利回り（TTM配当支払÷時価総額）", pct(d["div"] / E)), ("実効税率（TTM）", pct(t_eff)),
        ]
        for k, v in rows:
            print(f"| {k} | {v} |")
        print("\n### 2. 資本コスト（WACC）\n")
        print("| 項目 | 値 | 根拠 |\n|---|---|---|")
        print(f"| アンレバード・ベータ | {c['beta_u']:.2f} | Damodaran 業種別（2026年1月） |")
        print(f"| D/E（簿価負債÷時価総額） | {D/E:.2f} | 10-Q・株価 |")
        print(f"| レバード・ベータ | {beta_l:.2f} | βu×(1+(1−t)D/E) |")
        print(f"| 株主資本コスト | {pct(ke)} | rf + β×ERP |")
        print(f"| 負債コスト（税引前） | {pct(kd)} | rf + スプレッド（実績：支払利息÷有利子負債 = {pct(d['int']/D)}） |")
        print(f"| **WACC** | **{pct(wacc)}** | 時価ベースの加重 |")
        print("\n### 3. DCF（FCFF、10年＋終価）\n")
        print(f"基準のFCFF（TTM）＝FCF＋支払利息×(1−t)＝**${fcff/1e9:.2f}B**\n")
        print("| シナリオ | 1〜5年目成長率 | 永久成長率 | WACC | 理論株価 | 現在株価比 | 終価の比率 |\n|---|---|---|---|---|---|---|")
        vals_ = {}
        for s, (g1, gT, dw) in c["scen"].items():
            v, tvw = dcf(fcff, wacc + dw, g1, gT)
            px = (v - D + cash - (d["nci"] or 0)) / sh
            vals_[s] = px
            print(f"| {s} | {pct(g1)} | {pct(gT)} | {pct(wacc+dw)} | ${px:.2f} | {pct(px/P-1)} | {pct(tvw)} |")
        ev_px = sum(A["probs"][s] * vals_[s] for s in vals_)
        print(f"| **確率加重** | | | | **${ev_px:.2f}** | **{pct(ev_px/P-1)}** | |")
        print("\n### 3b. 基準年のFCFの選び方による違い（中立シナリオ）\n")
        adj = [a + b for a, b in zip(c["fcf_hist"], c["fcf_adj"])]
        bases = {"TTM（直近4四半期）": fcf / 1e9, "FY2025（一時的支出を足し戻し）": adj[-1],
                 "FY2021〜2025の平均（同）": sum(adj) / len(adj)}
        print("| 基準のFCF | FCF（$B） | FCFF（$B） | 理論株価 | 現在株価比 |\n|---|---|---|---|---|")
        g1, gT, dw = c["scen"]["中立"]
        for k, f in bases.items():
            ff = f * 1e9 + d["int"] * (1 - A["tax"])
            v, _ = dcf(ff, wacc + dw, g1, gT)
            px = (v - D + cash - (d["nci"] or 0)) / sh
            print(f"| {k} | {f:.2f} | {ff/1e9:.2f} | ${px:.2f} | {pct(px/P-1)} |")
        print("\n### 4. 感応度（中立シナリオの1〜5年目成長率を固定、理論株価）\n")
        g1b = c["scen"]["中立"][0]
        gts = [0.015, 0.020, 0.025, 0.030, 0.035]
        print("| WACC ＼ 永久成長率 | " + " | ".join(pct(x) for x in gts) + " |\n|---|" + "---|" * len(gts))
        for w in [wacc - 0.01, wacc - 0.005, wacc, wacc + 0.005, wacc + 0.01]:
            cells = []
            for gT in gts:
                v, _ = dcf(fcff, w, g1b, gT)
                cells.append(f"${(v - D + cash - (d['nci'] or 0)) / sh:.0f}")
            print(f"| {pct(w)} | " + " | ".join(cells) + " |")
        print("\n### 5. 逆DCF：現在の株価が織り込む成長率\n")
        target = ev
        for gT in [0.020, 0.025, 0.030]:
            g_imp = solve_g1(fcff, wacc, gT, target)
            avg = sum(a + b for a, b in zip(c["fcf_hist"], c["fcf_adj"])) / len(c["fcf_hist"])
            g_imp2 = solve_g1(avg * 1e9 + d["int"] * (1 - A["tax"]), wacc, gT, target)
            print(f"- WACC {pct(wacc)}、永久成長率 {pct(gT)} のとき、1〜5年目に必要なFCFF成長率：**{pct(g_imp)}**（TTM基準）／**{pct(g_imp2)}**（5年平均基準）")
        print("\n### 6. マルチプルによる確認\n")
        payout = d["div"] / d["ni"]
        for gT in [0.020, 0.025, 0.030]:
            jpe = payout * (1 + gT) / (ke - gT)
            print(f"- 正当化PER（配当性向 {pct(payout)}、株主資本コスト {pct(ke)}、成長率 {pct(gT)}）：{jpe:.1f}倍 → 株価 ${jpe*d['eps']:.2f}")
        print(f"- 益回り {pct(d['eps']/P)} − 米10年国債 5.29% ＝ {pct(d['eps']/P-0.0529)}（株式の上乗せ利回り。マイナスなら国債より利回りが低い）")


if __name__ == "__main__":
    main(sys.argv[1])
