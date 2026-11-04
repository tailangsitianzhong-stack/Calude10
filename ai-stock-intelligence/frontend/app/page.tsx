import Link from "next/link";
import { LineChart } from "@/components/LineChart";
import { SearchBox } from "@/components/SearchBox";
import { Card, Dash, ErrorNotice, Placeholder, StatusBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDay, fmtNum, marketLabel, signed } from "@/lib/format";
import type { History, IndexQuote, MacroItem } from "@/lib/types";

export const dynamic = "force-dynamic";

const MACRO_GROUPS: [string, string[]][] = [
  ["金利", ["us_ff_rate", "us_2y", "us_10y", "us_10y_real", "us_10y_3m", "us_baa_spread"]],
  ["インフレ・景気", ["us_cpi", "us_core_cpi", "us_breakeven_10y", "us_unemployment", "us_payrolls", "us_real_gdp"]],
  ["為替・商品・変動性", ["usdjpy", "eurusd", "usd_broad", "wti", "vix"]],
  ["日本", ["jp_10y", "jp_call"]],
];

function MacroRow({ m }: { m: MacroItem }) {
  const isCpi = m.yoy_pct !== undefined;
  const chg1y = m.value !== undefined && m.prev_1y !== null && m.prev_1y !== undefined
    ? (["%", "%pt"].includes(m.unit ?? "") ? m.value - m.prev_1y : (m.value / m.prev_1y - 1) * 100) : null;
  const ch = signed(isCpi ? null : chg1y, 2, ["%", "%pt"].includes(m.unit ?? "") ? "pt" : "%");
  return (
    <tr data-testid={`macro-${m.key}`}>
      <td className="py-2 pr-2">
        <div className="text-[13px]">{m.name}</div>
        <div className="text-[10px] text-faint">{m.source}{m.note ? `・${m.note}` : ""}</div>
      </td>
      <td className="num py-2 pr-2 text-right text-[13px]">
        {m.value === undefined ? <Dash /> : isCpi ? <>前年比 {fmtNum(m.yoy_pct, 2)}%</> : <>{fmtNum(m.value, m.value > 1000 ? 0 : 2)}<span className="ml-0.5 text-[10px] text-faint">{m.unit}</span></>}
      </td>
      <td className={`num py-2 pr-2 text-right text-[12px] ${ch.dir === "up" ? "text-up" : ch.dir === "down" ? "text-down" : "text-faint"}`}>{isCpi ? "" : ch.text}</td>
      <td className="py-2 text-right">
        <div className="num text-[11px] text-muted">{fmtDay(m.obs_date)}</div>
        {m.data_status !== "ok" && <StatusBadge status={m.data_status} />}
      </td>
    </tr>
  );
}

export default async function Dashboard() {
  const [ov, macro] = await Promise.all([api<IndexQuote[]>("/api/v1/market/overview"), api<{ series: MacroItem[] }>("/api/v1/macro")]);
  const hist = ov.ok
    ? await Promise.all(ov.data.map((q) => (q.value === null ? Promise.resolve(null) : api<History>(`/api/v1/market/indices/${q.key}/history`))))
    : [];
  const byKey = macro.ok ? Object.fromEntries(macro.data.series.map((m) => [m.key, m])) : {};
  return (
    <div className="space-y-6">
      <section className="space-y-4">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-accent">Dashboard</p>
          <h1 className="mt-1 text-2xl font-bold tracking-tight sm:text-3xl">マーケット概況</h1>
          <p className="mt-1 text-sm text-muted">取得できた実データだけを、取得元・基準日・遅延とあわせて表示します。リアルタイムの値ではありません。</p>
        </div>
        <SearchBox size="lg" />
      </section>

      {!ov.ok ? <ErrorNotice message={ov.error} /> : (
        <section aria-label="主要指数" className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {ov.data.map((q, i) => {
            const ch = signed(q.change_pct);
            const h = hist[i];
            return (
              <article key={q.key} data-testid={`index-${q.key}`} className="rounded-card border border-line bg-surface/80 p-4">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="text-[13px] font-semibold">{q.name}</div>
                    <div className="text-[11px] text-faint">{marketLabel(q.market)}</div>
                  </div>
                  <StatusBadge status={q.data_status} />
                </div>
                <div className="mt-3 flex items-baseline gap-2">
                  <span className="num text-2xl font-semibold">{q.value === null ? <Dash /> : fmtNum(q.value, 2)}</span>
                  {q.value !== null && <span className={`num text-[12px] ${ch.dir === "up" ? "text-up" : ch.dir === "down" ? "text-down" : "text-faint"}`}>{ch.text}</span>}
                </div>
                {h && h.ok && h.data.points.length > 1 && <div className="mt-2"><LineChart points={h.data.points} label={q.name} height={70} showAxis={false} /></div>}
                <div className="mt-2 space-y-0.5 text-[10.5px] leading-relaxed text-faint">
                  {q.value !== null && <div className="num text-muted">{fmtDay(q.as_of)} 終値・{q.provider}</div>}
                  <div>{q.note}</div>
                </div>
              </article>
            );
          })}
        </section>
      )}

      <Card title="経済指標・金利・為替" aside={<span className="text-[11px] text-faint">出所：FRED（セントルイス連銀）。前年差・前年比は1年前の値との比較</span>}>
        {!macro.ok ? <ErrorNotice message={macro.error} /> : (
          <div className="grid gap-x-8 gap-y-4 lg:grid-cols-2">
            {MACRO_GROUPS.map(([g, keys]) => (
              <div key={g}>
                <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">{g}</h3>
                <table className="w-full">
                  <tbody className="divide-y divide-line/60">
                    {keys.filter((k) => byKey[k]).map((k) => <MacroRow key={k} m={byKey[k]} />)}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="AI総合評価ランキング（上位）" className="lg:col-span-1" aside={<Link href="/rankings" className="text-xs text-accent hover:underline">すべて見る</Link>}>
          <Placeholder phase={4}>100点満点の採点の上位銘柄を表示します。採点には株価データ（J-Quants・米国株API）の接続が必要です。</Placeholder>
        </Card>
        <Card title="上昇予測・下落警戒">
          <Placeholder phase={5}>上昇・横ばい・下落の確率を、検証成績と信頼度とあわせて表示します。</Placeholder>
        </Card>
        <Card title="重要な開示・ニュース">
          <Placeholder phase={4}>米国の8-K、日本の適時開示などの一次情報の件名と、原文へのリンクを表示します。</Placeholder>
        </Card>
      </div>
    </div>
  );
}
