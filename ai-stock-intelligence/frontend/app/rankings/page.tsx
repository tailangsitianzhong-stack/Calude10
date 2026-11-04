import { Card, Placeholder } from "@/components/ui";

export const metadata = { title: "AIランキング" };

const TABS = [
  ["総合評価ランキング", "100点満点の総合点の高い順。採点できたデータの割合（カバー率）と信頼度を併記"],
  ["成長性ランキング", "成長性（20点）の高い順。業種テンプレート内の順位で比較"],
  ["割安株ランキング", "企業価値・割安度（15点）の高い順。逆算した市場の期待と実績の差を重視"],
  ["上昇予測ランキング", "上昇確率の高い順。検証済みの信頼度とあわせて表示"],
  ["下落警戒ランキング", "下落確率・下落シナリオの大きい順"],
  ["高配当ランキング", "配当利回りの高い順。配当の持続性（FCFに対する配当の比率）を併記"],
];

export default function Rankings() {
  return (
    <div className="space-y-5">
      <div>
        <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-accent">Rankings</p>
        <h1 className="mt-1 text-2xl font-bold tracking-tight">AIランキング</h1>
        <p className="mt-1 text-sm text-muted">採点（フェーズ4）と予測（フェーズ5）の実装後に表示します。現在はランキングを作成していません。</p>
      </div>
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {TABS.map(([t, d], i) => (
          <Card key={t} title={t}><Placeholder phase={i >= 3 && i <= 4 ? 5 : 4}>{d}</Placeholder></Card>
        ))}
      </div>
    </div>
  );
}
