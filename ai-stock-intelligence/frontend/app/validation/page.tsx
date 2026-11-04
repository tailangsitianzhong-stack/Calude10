import { Card, Placeholder } from "@/components/ui";

export const metadata = { title: "予測検証" };

export default function Validation() {
  return (
    <div className="space-y-5">
      <div>
        <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-accent">Validation</p>
        <h1 className="mt-1 text-2xl font-bold tracking-tight">予測検証</h1>
        <p className="mt-1 text-sm text-muted">AIが出した予測と実際の株価を比べ、精度を公開します。予測はまだ1件も記録していません。</p>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <Card title="過去の予測と結果"><Placeholder phase={6}>予測日・予測時の株価・予測内容・対象期間・実際の株価・結果・誤差。記録は追記のみで、後から書き換えられません（データベースの制約とハッシュ連鎖）。</Placeholder></Card>
        <Card title="確率の較正（キャリブレーション）"><Placeholder phase={6}>「上昇確率70%」と予測した銘柄群が、実際に約70%上昇したかを確率帯ごとに表示します。</Placeholder></Card>
        <Card title="モデル別の精度"><Placeholder phase={6}>ベースライン（市場平均）・モメンタム・ファンダメンタルズ・市場環境・機械学習・アンサンブルの比較。ブライアスコアと方向の的中率。</Placeholder></Card>
        <Card title="市場環境別の精度"><Placeholder phase={6}>上昇相場・下落相場、高インフレ・低インフレ、高ボラ・低ボラ別の成績。</Placeholder></Card>
      </div>
    </div>
  );
}
