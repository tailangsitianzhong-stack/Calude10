"""第9段階の投資判断用の補助計算：会社予想ベースのDCF、下落シナリオ、生活必需品株の過去の下落。

使い方:
    python3 -I tools/investment_case.py <companyfacts JSONのディレクトリ> <12業種CSVのパス>

前提は valuation.py の ASSUMPTIONS（株価・金利・ERP・ベータ・シナリオ）を共有し、
基準のFCFだけを各社の2026年度の会社予想（2026年10月の決算発表）に置き換える。
"""
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import valuation as V  # noqa: E402
from macro_analysis import french_block  # noqa: E402

FORWARD_FCF = {
    # KO：2026年度の会社予想 FCF「130億ドル以上」（2026-10-27 決算発表）
    "ko": (13.0e9, "会社予想 FY2026 FCF ≥ $13.0B"),
    # PEP：会社予想は「FCF転換率（FCF÷コア純利益）80%以上」。コアEPS = FY2025 $8.14 ×(1+3%)、株数1.363十億株で概算
    "pep": (0.80 * 8.14 * 1.03 * 1.3627e9, "会社予想の下限：コア純利益×80%（概算）"),
}
STRESS_PE = {"ko": [24, 20, 16], "pep": [15, 13, 11]}  # 下落シナリオで想定するPER（筆者の仮定）
EPS_SHOCK = [0.0, -0.10, -0.20]


def main(base, ind_path):
    A = V.ASSUMPTIONS
    for tk, c in A["companies"].items():
        d = V.load(f"{base}/{tk}.json")
        P, sh, D, cash = c["price"], d["shares"], d["debt"], d["cash_all"]
        E = P * sh
        beta_l = c["beta_u"] * (1 + (1 - A["tax"]) * D / E)
        ke = A["rf"] + beta_l * A["erp"]
        wacc = (E * ke + D * (A["rf"] + A["debt_spread"]) * (1 - A["tax"])) / (E + D)
        f, note = FORWARD_FCF[tk]
        fcff = f + d["int"] * (1 - A["tax"])
        print(f"\n## {c['name']}（{tk.upper()}）\n")
        print(f"基準のFCF：**${f/1e9:.2f}B**（{note}）、FCFF ${fcff/1e9:.2f}B、WACC {V.pct(wacc)}\n")
        print("| シナリオ | 理論株価 | 現在株価比 |\n|---|---|---|")
        tot = 0
        for s, (g1, gT, dw) in c["scen"].items():
            v, _ = V.dcf(fcff, wacc + dw, g1, gT)
            px = (v - D + cash - (d["nci"] or 0)) / sh
            tot += A["probs"][s] * px
            print(f"| {s} | ${px:.2f} | {V.pct(px/P-1)} |")
        print(f"| **確率加重** | **${tot:.2f}** | **{V.pct(tot/P-1)}** |")
        gi = V.solve_g1(fcff, wacc, 0.025, E + D - cash + (d["nci"] or 0))
        print(f"\n逆DCF（永久成長率2.5%）：1〜5年目に必要なFCFF成長率 **{V.pct(gi)}**\n")
        print("### 下落シナリオ（PERの縮小 × EPSの減少、TTM EPS基準）\n")
        print("| PER ＼ EPSの変化 | " + " | ".join(f"{int(x*100)}%" for x in EPS_SHOCK) + " |\n|---|" + "---|" * len(EPS_SHOCK))
        for pe in STRESS_PE[tk]:
            cells = [f"${pe*d['eps']*(1+x):.0f}（{V.pct(pe*d['eps']*(1+x)/P-1)}）" for x in EPS_SHOCK]
            print(f"| {pe}倍 | " + " | ".join(cells) + " |")
        print(f"\n（現在のPER {P/d['eps']:.1f}倍、TTM EPS ${d['eps']:.2f}）")

    ind = french_block(ind_path, "Average Value Weighted Returns -- Monthly")
    nd = ind["NoDur"].dropna()
    print("\n## 米国の非耐久消費財（生活必需品）業種：過去の下落（French 12業種、時価加重、総リターン、月次）\n")
    eq = (1 + nd).cumprod()
    dd = eq / eq.cummax() - 1
    print(f"- 全期間（{nd.index[0]}〜{nd.index[-1]}）の最大DD：{dd.min()*100:.1f}%（{dd.idxmin()}）")
    for a, b, name in [("1972-12", "1974-12", "1973〜74年"), ("1998-06", "2000-03", "1998〜2000年（ITバブル期）"),
                       ("2007-10", "2009-02", "2007〜09年"), ("2022-01", "2022-09", "2022年")]:
        x = nd.loc[a:b]
        e = (1 + x).cumprod()
        print(f"- {name}：期間中の最大下落 {((e / e.cummax()) - 1).min()*100:.1f}%、累積 {(e.iloc[-1]-1)*100:.1f}%")
    roll = (1 + nd).rolling(60).apply(np.prod, raw=True) ** (12 / 60) - 1
    print(f"- 最悪の5年間（年率）：{roll.min()*100:.1f}%（{roll.idxmin()}に終わる5年）")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
