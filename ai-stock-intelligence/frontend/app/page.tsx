import Link from "next/link";
import { SearchBox } from "@/components/SearchBox";
import { Card, Dash, ErrorNotice, Placeholder, StatusBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDay, marketLabel } from "@/lib/format";
import type { IndexQuote, SystemStatus } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function Dashboard() {
  const [ov, st] = await Promise.all([api<IndexQuote[]>("/api/v1/market/overview"), api<SystemStatus>("/api/v1/system/status")]);
  return (
    <div className="space-y-6">
      <section className="space-y-4">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-accent">Dashboard</p>
          <h1 className="mt-1 text-2xl font-bold tracking-tight sm:text-3xl">マーケット概況</h1>
          <p className="mt-1 text-sm text-muted">主要指数・市場環境・AIの注目銘柄。データ未接続の項目は値を表示しません。</p>
        </div>
        <SearchBox size="lg" />
      </section>

      {!ov.ok ? <ErrorNotice message={ov.error} /> : (
        <section aria-label="主要指数" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {ov.data.map((q) => (
            <article key={q.key} data-testid={`index-${q.key}`} className="rounded-card border border-line bg-surface/80 p-4">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="text-[13px] font-semibold">{q.name}</div>
                  <div className="text-[11px] text-faint">{marketLabel(q.market)}</div>
                </div>
                <StatusBadge status={q.data_status} />
              </div>
              <div className="num mt-4 text-2xl font-semibold">{q.value === null ? <Dash /> : q.value.toLocaleString()}</div>
              <div className="mt-1 text-[11px] text-faint">{q.value === null ? q.note : `${q.provider}・${q.as_of}`}</div>
            </article>
          ))}
        </section>
      )}

      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="市場環境（金利・インフレ・為替）" className="lg:col-span-1">
          <Placeholder phase={3}>FF金利、米10年国債、CPI、ドル円、日本の長期金利などを公的データ（FRED・日銀）から取得し、局面を判定して表示します。</Placeholder>
        </Card>
        <Card title="AI総合評価ランキング（上位）" className="lg:col-span-2" aside={<Link href="/rankings" className="text-xs text-accent hover:underline">すべて見る</Link>}>
          <Placeholder phase={4}>100点満点の採点（9項目・業種別の基準・信頼度つき）の上位銘柄を表示します。採点できたデータの割合が70%未満の銘柄は表示しません。</Placeholder>
        </Card>
        <Card title="上昇予測（確率の高い銘柄）">
          <Placeholder phase={5}>上昇・横ばい・下落の確率を、検証成績と信頼度とあわせて表示します。予測力が市場平均と同程度の間は、その旨を明示します。</Placeholder>
        </Card>
        <Card title="下落警戒">
          <Placeholder phase={5}>下落確率・下落シナリオの大きい銘柄を表示します。</Placeholder>
        </Card>
        <Card title="重要な開示・ニュース">
          <Placeholder phase={3}>米国の8-K、日本の適時開示などの一次情報の件名と、原文へのリンクを表示します。</Placeholder>
        </Card>
      </div>

      <Card title="データ接続の状態" aside={<span className="text-[11px] text-faint">設定はサーバー側の .env で行います（ブラウザには送られません）</span>}>
        {!st.ok ? <ErrorNotice message={st.error} /> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-left text-sm">
              <thead className="text-[11px] uppercase tracking-wider text-faint">
                <tr><th className="pb-2 font-medium">データ提供元</th><th className="pb-2 font-medium">対象</th><th className="pb-2 font-medium">状態</th><th className="pb-2 font-medium">備考</th></tr>
              </thead>
              <tbody className="divide-y divide-line/70">
                {st.data.providers.map((p) => (
                  <tr key={p.key} data-testid={`provider-${p.key}`}>
                    <td className="py-2.5 pr-3 font-medium">{p.name}</td>
                    <td className="py-2.5 pr-3 text-xs text-muted">{p.datasets.join(", ")}</td>
                    <td className="py-2.5 pr-3"><StatusBadge status={p.status} /></td>
                    <td className="py-2.5 text-xs text-muted">
                      {p.message}{p.missing_settings.length > 0 && <span className="text-faint">（未設定：{p.missing_settings.join(", ")}）</span>}
                    </td>
                  </tr>
                ))}
                {st.data.datasets.map((d) => (
                  <tr key={d.dataset}>
                    <td className="py-2.5 pr-3 font-medium">{d.source}</td>
                    <td className="py-2.5 pr-3 text-xs text-muted">{d.dataset}</td>
                    <td className="py-2.5 pr-3"><StatusBadge status="ok" /></td>
                    <td className="py-2.5 text-xs text-muted"><span className="num">{d.rows.toLocaleString()}</span>件・{fmtDay(d.source_as_of)}時点</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
