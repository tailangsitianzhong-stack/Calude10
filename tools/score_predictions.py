"""予測の記録（predictions/prediction_log.csv）を採点する（第10段階）。

使い方:
    python3 -I tools/score_predictions.py predictions/prediction_log.csv [基準日 YYYY-MM-DD]

- 「結果」列に 1（的中）または 0（外れ）を記入した予測をブライアスコア (p − o)² で採点する。
  0が完全、0.25は常に50%と答えるのと同じ。
- 確率帯（50〜60%、60〜70%…）ごとに、予測した確率と実際の的中率を比べる（キャリブレーション）。
- 未判定の予測は判定期限順に一覧表示し、期限を過ぎたものに印を付ける。
"""
import csv
import sys
from datetime import date


def main(path, today=None):
    today = date.fromisoformat(today) if today else date.today()
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    done = [r for r in rows if r["結果"].strip() in ("0", "1")]
    pend = [r for r in rows if r not in done]
    print(f"# 予測の採点（基準日 {today}）\n")
    print(f"記録 {len(rows)}件：判定済み {len(done)}件、未判定 {len(pend)}件\n")
    if done:
        bs = [(float(r["確率"]) - int(r["結果"])) ** 2 for r in done]
        print(f"## ブライアスコア：{sum(bs)/len(bs):.3f}（0が最良、常に50%なら0.250）\n")
        print("| 確率帯 | 件数 | 予測確率の平均 | 実際の的中率 |\n|---|---|---|---|")
        for lo in [0.5, 0.6, 0.7, 0.8, 0.9]:
            b = [r for r in done if lo <= max(float(r["確率"]), 1 - float(r["確率"])) < lo + 0.1 + (1e-9 if lo == 0.9 else 0)]
            if b:
                # 確率が0.5未満の予測は「起きない」側に読み替えて集計する
                ps = [max(float(r["確率"]), 1 - float(r["確率"])) for r in b]
                hits = [int(r["結果"]) if float(r["確率"]) >= 0.5 else 1 - int(r["結果"]) for r in b]
                print(f"| {int(lo*100)}〜{int(lo*100)+10}% | {len(b)} | {sum(ps)/len(ps)*100:.0f}% | {sum(hits)/len(hits)*100:.0f}% |")
        print()
    print("## 未判定の予測（判定期限順）\n")
    print("| ID | 判定期限 | 確率 | 予測 | 状態 |\n|---|---|---|---|---|")
    for r in sorted(pend, key=lambda r: r["判定期限"]):
        due = date.fromisoformat(r["判定期限"])
        st = "**期限超過：判定が必要**" if due < today else f"あと{(due - today).days}日"
        print(f"| {r['id']} | {r['判定期限']} | {float(r['確率'])*100:.0f}% | {r['対象']}：{r['予測（検証可能な文）']} | {st} |")
    exp = sum(float(r["確率"]) for r in rows)
    print(f"\n参考：記録した全予測の確率の合計 ＝ {exp:.1f}（予測どおりなら、{len(rows)}件のうち約{exp:.0f}件が的中するはず）")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
