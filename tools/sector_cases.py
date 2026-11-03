"""第9段階（追加）：業種によって評価方法が変わることの検証。
  - Micron Technology（MU）：景気循環の半導体（メモリ）→ サイクルを通した「正常化利益」で評価
  - JPMorgan Chase（JPM）：銀行 → DCFではなく ROE（ROTCE）と株主資本コストによる正当化PBR（P/TBV）で評価

使い方:
    python3 -I tools/sector_cases.py <companyfacts JSONのディレクトリ>
前提は ASSUMPTIONS に集約（株価：2026-11-03終値、rf・ERP：Damodaran 2026-11-01、ベータ：Damodaran 業種別 2026年1月）。
"""
import json
import sys
from datetime import date

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from valuation import ttm  # noqa: E402

ASSUMPTIONS = {
    "rf": 0.0507, "erp": 0.0463,
    # guide_q_eps：2027年度第1四半期の会社予想 非GAAP EPS $38.15（2026年9月の決算発表）
    "mu": {"price": None, "beta_u": None, "tax_norm": 0.15, "guide_q_eps": 38.15},
    "jpm": {"price": None, "beta_l": None},
}


def dur(v):
    return (date.fromisoformat(v["end"]) - date.fromisoformat(v["start"])).days


def annual(g, tags, unit="USD"):
    """10-Kの年次値（各年度末の最新提出分）。複数タグは先頭優先で補完。"""
    out = {}
    for t in tags:
        if t not in g:
            continue
        best = {}
        for v in g[t]["units"].get(unit, []):
            if not v.get("form", "").startswith("10-K") or "start" not in v or not 350 <= dur(v) <= 380:
                continue
            if v["end"] not in best or v["filed"] > best[v["end"]]["filed"]:
                best[v["end"]] = v
        for k, v in best.items():
            out.setdefault(k[:7], v["val"])
    return out


def instant(g, tags, unit="USD", form="10-K"):
    out = {}
    for t in tags:
        if t not in g:
            continue
        best = {}
        for v in g[t]["units"].get(unit, []):
            if not v.get("form", "").startswith(form) or "start" in v:
                continue
            if v["end"] not in best or v["filed"] > best[v["end"]]["filed"]:
                best[v["end"]] = v
        for k, v in best.items():
            out.setdefault(k[:7], v["val"])
    return out


def pct(x):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x*100:.1f}%"


def micron(path, price, beta_u):
    A = ASSUMPTIONS
    f = json.load(open(path))["facts"]
    g = f["us-gaap"]
    rev = annual(g, ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"])
    gp, op, ni = annual(g, ["GrossProfit"]), annual(g, ["OperatingIncomeLoss"]), annual(g, ["NetIncomeLoss"])
    cfo, capex = annual(g, ["NetCashProvidedByUsedInOperatingActivities"]), annual(g, ["PaymentsToAcquirePropertyPlantAndEquipment"])
    eps = annual(g, ["EarningsPerShareDiluted"], "USD/shares")
    inv, eq = instant(g, ["InventoryNet"]), instant(g, ["StockholdersEquity"])
    tax, pre = annual(g, ["IncomeTaxExpenseBenefit"]), annual(g, ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"])
    yrs = sorted(k for k in rev if k in op and k in ni)[-15:]
    print("## Micron Technology（MU）：15年間の業績サイクル（10-K、会計年度は8〜9月末）\n")
    print("| 年度末 | 売上（$B） | 売上成長 | 粗利率 | 営業利益率 | 純利益（$B） | 希薄化EPS | FCF（$B） | 設備投資/売上 | 在庫日数 | ROE |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    prev = None
    rows = []
    for y in yrs:
        cogs = rev[y] - gp.get(y, np.nan)
        fcf = cfo.get(y, np.nan) - capex.get(y, np.nan)
        invd = inv.get(y, np.nan) / cogs * 365 if cogs else np.nan
        roe = ni[y] / eq[y] if y in eq else np.nan
        gr = rev[y] / rev[prev] - 1 if prev else np.nan
        rows.append({"y": y, "rev": rev[y], "gm": gp.get(y, np.nan) / rev[y], "om": op[y] / rev[y], "nm": ni[y] / rev[y],
                     "capex": capex.get(y, np.nan) / rev[y], "fcf": fcf, "roe": roe, "eps": eps.get(y, np.nan)})
        print(f"| {y} | {rev[y]/1e9:.2f} | {pct(gr) if prev else '—'} | {pct(gp.get(y, np.nan)/rev[y])} | {pct(op[y]/rev[y])} | "
              f"{ni[y]/1e9:.2f} | {eps.get(y, np.nan):.2f} | {fcf/1e9:.2f} | {pct(capex.get(y, np.nan)/rev[y])} | {invd:.0f} | {pct(roe)} |")
        prev = y
    last = rows[-1]
    r10 = rows[-10:]
    om_avg = np.mean([r["om"] for r in r10])
    nm_avg = np.mean([r["nm"] for r in r10])
    capex_avg = np.mean([r["capex"] for r in r10])
    rev_cagr = (rows[-1]["rev"] / rows[-11]["rev"]) ** (1 / 10) - 1
    sh = f["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
    shares = max(sh, key=lambda x: x["end"])["val"]
    cash = instant(g, ["CashAndCashEquivalentsAtCarryingValue"])
    debt_nc = instant(g, ["LongTermDebtNoncurrent"])
    debt_c = instant(g, ["LongTermDebtCurrent", "DebtCurrent"])
    sti = instant(g, ["ShortTermInvestments", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"])
    lti = instant(g, ["LongTermInvestments", "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent"])
    y = last["y"]
    D = debt_nc.get(y, 0) + debt_c.get(y, 0)
    C = cash.get(y, 0) + sti.get(y, 0) + lti.get(y, 0)
    E = price * shares
    beta_l = beta_u * (1 + (1 - 0.15) * D / E)
    ke = A["rf"] + beta_l * A["erp"]
    wacc = (E * ke + D * (A["rf"] + 0.012) * (1 - 0.15)) / (E + D)
    print(f"\n**サイクルを通した平均（直近10年度）**：営業利益率 {pct(om_avg)}、純利益率 {pct(nm_avg)}、設備投資/売上 {pct(capex_avg)}、"
          f"売上の10年CAGR {pct(rev_cagr)}\n")
    print("### 現在の評価\n")
    print("| 項目 | 値 |\n|---|---|")
    print(f"| 株価（2026-11-03終値） | ${price:.2f} |\n| 発行済株式数 | {shares/1e9:.3f}十億株 |\n| 時価総額 | ${E/1e9:.1f}B |")
    print(f"| 有利子負債 / 現金・投資 | ${D/1e9:.1f}B / ${C/1e9:.1f}B |\n| 企業価値（EV） | ${(E+D-C)/1e9:.1f}B |")
    print(f"| PER（直近年度EPS ${last['eps']:.2f}） | {price/last['eps']:.1f}倍 |\n| EV/売上（直近年度） | {(E+D-C)/last['rev']:.2f}倍 |")
    print(f"| PBR | {E/eq[y]:.2f}倍 |\n| 直近年度の営業利益率（10年平均との差） | {pct(last['om'])}（{(last['om']-om_avg)*100:+.1f}pt） |")
    print(f"| レバード・ベータ / 株主資本コスト / WACC | {beta_l:.2f} / {pct(ke)} / {pct(wacc)} |")
    print("\n### 正常化利益による評価（バリュー・ドライバー式：価値 ＝ NOPAT×(1−g/ROIC)÷(WACC−g)）\n")
    print("「直近年度の売上 × 想定営業利益率 × (1−税率)」を正常なNOPATとし、永久成長率g、再投資の効率ROICで評価。\n")
    print("| シナリオ | 正常化売上 | 営業利益率 | g | ROIC | 理論株価 | 現在株価比 |\n|---|---|---|---|---|---|---|")
    scen = {"弱気（サイクルの谷に近い売上・利益率）": (0.70, om_avg * 0.6, 0.03, 0.12),
            "中立（直近売上×10年平均の利益率）": (1.00, om_avg, 0.04, 0.15),
            "強気（直近売上×直近の利益率が続く）": (1.00, last["om"], 0.05, 0.20)}
    res = {}
    for k, (rs, m, gg, roic) in scen.items():
        nopat = last["rev"] * rs * m * (1 - A["mu"]["tax_norm"])
        v = nopat * (1 - gg / roic) / (wacc - gg)
        px = (v - D + C) / shares
        res[k] = px
        print(f"| {k} | ${last['rev']*rs/1e9:.1f}B | {pct(m)} | {pct(gg)} | {pct(roic)} | ${px:.2f} | {pct(px/price-1)} |")
    # 好況が何年続くと現在の株価になるか：好況期は会社予想（次四半期の非GAAP EPS×4）のEPSに
    # FCF/純利益の比率（直近年度）を掛けたFCFが続き、その後は「中立」シナリオの価値（現金を除く）に戻る
    eps_run = A["mu"]["guide_q_eps"] * 4
    conv = last["fcf"] / (last["nm"] * last["rev"])
    cash_ps = (C - D) / shares
    normal_ex_cash = res["中立（直近売上×10年平均の利益率）"] - cash_ps
    print(f"\n### 逆算：好況（ピーク利益）が何年続く前提か\n")
    print(f"好況期の1株FCF＝会社予想の次四半期EPS ${A['mu']['guide_q_eps']:.2f}×4×FCF転換率{pct(conv)}＝${eps_run*conv:.2f}。"
          f"好況の後は中立シナリオの事業価値（1株${normal_ex_cash:.2f}）に戻ると仮定。割引率 {pct(ke)}。\n")
    print("| 好況の継続年数 | 理論株価 | 現在株価比 |\n|---|---|---|")
    for n in [1, 2, 3, 4, 6, 8, 10]:
        pv = sum(eps_run * conv / (1 + ke) ** t for t in range(1, n + 1)) + normal_ex_cash / (1 + ke) ** n + cash_ps
        print(f"| {n}年 | ${pv:.2f} | {pct(pv/price-1)} |")
    pe_trough = min(r["eps"] for r in r10 if r["eps"] > 0)
    print(f"\n- 直近10年度でEPSが最も低かった（プラスの）年のEPS：${pe_trough:.2f}。赤字の年：{sum(1 for r in r10 if r['eps'] < 0)}年")
    print(f"- 現在の株価は、直近年度のEPSの{price/last['eps']:.1f}倍、10年平均純利益率で正常化したEPS（${last['rev']*nm_avg/shares:.2f}）の{price/(last['rev']*nm_avg/shares):.1f}倍")
    return res


def jpm(path, price, beta_l):
    A = ASSUMPTIONS
    f = json.load(open(path))["facts"]
    g = f["us-gaap"]
    ni_c = annual(g, ["NetIncomeLossAvailableToCommonStockholdersBasic"])
    ni = annual(g, ["NetIncomeLoss"])
    rev = annual(g, ["RevenuesNetOfInterestExpense", "Revenues"])
    nii, prov = annual(g, ["InterestIncomeExpenseNet"]), annual(g, ["ProvisionForLoanLeaseAndOtherLosses", "ProvisionForLoanAndLeaseLosses"])
    nie = annual(g, ["NoninterestExpense"])
    eps = annual(g, ["EarningsPerShareDiluted"], "USD/shares")
    eq, pref, gw = instant(g, ["StockholdersEquity"]), instant(g, ["PreferredStockIncludingAdditionalPaidInCapitalNetOfDiscount", "PreferredStockValue"]), instant(g, ["Goodwill"])
    assets = instant(g, ["Assets"])
    yrs = sorted(k for k in ni if k in eq)[-12:]
    print("\n## JPMorgan Chase（JPM）：12年間の収益性（10-K）\n")
    print("| 年度末 | 純営業収益（$B） | 純金利収益（$B） | 信用コスト（$B） | 経費率 | 普通株主に帰属する純利益（$B） | 希薄化EPS | ROE（普通株） | ROTCE（近似） | ROA |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    hist = []
    for y in yrs:
        ce = eq[y] - pref.get(y, 0)
        tce = ce - gw.get(y, 0)
        nic = ni_c.get(y, ni[y] - 0)
        roe, rotce = nic / ce, nic / tce
        hist.append(rotce)
        print(f"| {y} | {rev.get(y, np.nan)/1e9:.1f} | {nii.get(y, np.nan)/1e9:.1f} | {prov.get(y, np.nan)/1e9:.1f} | {pct(nie.get(y, np.nan)/rev.get(y, np.nan))} | "
              f"{nic/1e9:.1f} | {eps.get(y, np.nan):.2f} | {pct(roe)} | {pct(rotce)} | {pct(ni[y]/assets.get(y, np.nan))} |")
    q = [x for x in g["NetIncomeLoss"]["units"]["USD"] if x["form"] == "10-Q"]
    q_accn = max(q, key=lambda x: x["filed"])["accn"]
    ttm_nic, note = ttm(g, ["NetIncomeLossAvailableToCommonStockholdersBasic"], q_accn)
    ttm_eps, _ = ttm(g, ["EarningsPerShareDiluted"], q_accn)
    ttm_prov, _ = ttm(g, ["ProvisionForLoanLeaseAndOtherLosses", "ProvisionForLoanAndLeaseLosses"], q_accn)
    def qi(tags):
        """最新10-Qの期末値。そのタグが10-Qにない場合は、全提出書類の最新の期末値で代用する。"""
        cand = [x for t in tags if t in g for x in g[t]["units"]["USD"] if x.get("accn") == q_accn and "start" not in x]
        if not cand:
            cand = [x for t in tags if t in g for x in g[t]["units"]["USD"] if "start" not in x]
        return max(cand, key=lambda x: (x["end"], x["filed"]))["val"]
    ce_now = qi(["StockholdersEquity"]) - qi(["PreferredStockIncludingAdditionalPaidInCapitalNetOfDiscount"])
    tce_now = ce_now - qi(["Goodwill"])
    sh = f["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
    shares = max(sh, key=lambda x: x["end"])["val"]
    bvps, tbvps = ce_now / shares, tce_now / shares
    ke = A["rf"] + beta_l * A["erp"]
    rotce_ttm = ttm_nic / tce_now
    print(f"\nTTM（{note}）：普通株主に帰属する純利益 ${ttm_nic/1e9:.1f}B、希薄化EPS ${ttm_eps:.2f}、信用コスト ${ttm_prov/1e9:.1f}B\n")
    print("### 現在の評価\n")
    print("| 項目 | 値 |\n|---|---|")
    print(f"| 株価（2026-11-03終値） | ${price:.2f} |\n| 時価総額 | ${price*shares/1e9:.1f}B |")
    print(f"| 1株純資産（普通株） / 1株有形純資産（のれん控除の近似） | ${bvps:.2f} / ${tbvps:.2f} |")
    print(f"| PER（TTM） | {price/ttm_eps:.1f}倍 |\n| PBR / P/TBV | {price/bvps:.2f}倍 / {price/tbvps:.2f}倍 |")
    print(f"| ROTCE（TTM、期末の有形普通株主資本） | {pct(rotce_ttm)} |\n| 株主資本コスト（β {beta_l:.2f}） | {pct(ke)} |")
    print(f"| 過去12年のROTCE（近似）：平均 / 最低 / 最高 | {pct(np.mean(hist))} / {pct(min(hist))} / {pct(max(hist))} |")
    print("\n### 正当化P/TBV ＝（ROTCE − g）÷（株主資本コスト − g）\n")
    print("| シナリオ | 持続的なROTCE | g | 株主資本コスト | 正当化P/TBV | 理論株価 | 現在株価比 |\n|---|---|---|---|---|---|---|")
    res = {}
    for k, (r_, gg, dk) in {"弱気（信用コストの上昇・利ざや縮小）": (0.13, 0.03, 0.005), "中立（過去12年の平均並み）": (float(np.mean(hist)), 0.04, 0.0),
                            "強気（直近の水準が続く）": (rotce_ttm, 0.045, -0.005)}.items():
        pb = (r_ - gg) / (ke + dk - gg)
        px = pb * tbvps
        res[k] = px
        print(f"| {k} | {pct(r_)} | {pct(gg)} | {pct(ke+dk)} | {pb:.2f}倍 | ${px:.2f} | {pct(px/price-1)} |")
    # 逆算：現在のP/TBVが前提とする持続的ROTCE（g=4%）
    r_imp = price / tbvps * (ke - 0.04) + 0.04
    print(f"\n逆算：現在のP/TBV {price/tbvps:.2f}倍が正当化されるには、g=4%で**持続的なROTCE {pct(r_imp)}**が必要")
    return res


if __name__ == "__main__":
    base = sys.argv[1]
    mu_price, mu_beta_u, jpm_price, jpm_beta = map(float, sys.argv[2:6])
    micron(f"{base}/mu.json", mu_price, mu_beta_u)
    jpm(f"{base}/jpm.json", jpm_price, jpm_beta)
