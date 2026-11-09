import Link from "next/link";
import { notFound } from "next/navigation";
import { FinancialsCard } from "@/components/FinancialsCard";
import { LineChart } from "@/components/LineChart";
import { ScoreGauge } from "@/components/ScoreGauge";
import { Card, Dash, ErrorNotice, Placeholder, StatusBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDate, fmtDay, marketLabel, typeLabel } from "@/lib/format";
import type { Financials, Section, SecurityDetail } from "@/lib/types";

export const dynamic = "force-dynamic";

type Params = Promise<{ market: string; code: string }>;

export async function generateMetadata({ params }: { params: Params }) {
  const { market, code } = await params;
  return { title: `${decodeURIComponent(code)}（${marketLabel(market.toUpperCase())}）` };
}

// 株価チャートが空のときの説明（株価の接続状態ごと。APIキーが設定済みなのに「未設定」と表示しない）
const CHART_EMPTY: Record<string, string> = {
  not_configured: "株価のAPIキーが未設定のため、チャートは表示しません。",
  configured_not_fetched: "APIキーは設定済みですが、この銘柄の株価はまだ取得していません。",
  provider_error: "株価の取得元の障害で取得できていないため、チャートは表示しません。",
};

const ANCHORS: Record<string, string> = {
  "AI総合評価（100点満点）": "score", "テクニカル分析": "technicals", "ニュース・開示": "news",
  "競合比較": "peers", "理論株価": "valuation", "将来株価予測": "forecast", "リスク分析": "risk", "AIによる総合コメント": "commentary",
};

export default async function StockPage({ params }: { params: Params }) {
  const { market, code } = await params;
  const path = `/api/v1/securities/${encodeURIComponent(market.toUpperCase())}/${encodeURIComponent(decodeURIComponent(code))}`;
  const [d, secs, fin, px] = await Promise.all([
    api<SecurityDetail>(path), api<Section[]>(`${path}/sections`), api<Financials>(`${path}/financials`),
    api<{ data_status: string; price_state?: string; price_state_label?: string; provider: string | null; points: [string, number, number | null][] }>(`${path}/prices`),
  ]);
  if (!d.ok && d.status === 404) notFound();
  if (!d.ok) return <ErrorNotice message={d.error} />;
  const { security: s, quote } = d.data;
  const sections = secs.ok ? secs.data : [];
  return (
    <div className="space-y-5">
      <nav className="text-xs text-faint" aria-label="パンくず">
        <Link href="/search" className="hover:text-ink">銘柄検索</Link> <span aria-hidden>/</span> {marketLabel(s.market)} <span aria-hidden>/</span> {s.code}
      </nav>

      <section className="grid gap-4 rounded-card border border-line bg-surface/80 p-5 md:grid-cols-[1fr_auto_auto] md:items-center" aria-label="銘柄の概要">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`num rounded-md px-2 py-1 text-xs font-semibold ${s.market === "JP" ? "bg-accent/15 text-accent" : "bg-up/15 text-up"}`}>{s.code}</span>
            <span className="text-xs text-faint">{marketLabel(s.market)}・{s.exchange ?? "—"}</span>
            {!s.is_active && <StatusBadge status="stale">上場一覧に掲載なし</StatusBadge>}
          </div>
          <h1 className="mt-2 text-xl font-bold leading-snug tracking-tight sm:text-2xl" data-testid="security-name">{s.name}</h1>
          <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
            <div><dt className="inline text-faint">業種 </dt><dd className="inline">{s.sector ?? "未取得"}</dd></div>
            <div><dt className="inline text-faint">規模 </dt><dd className="inline">{s.size_class ?? "—"}</dd></div>
            <div><dt className="inline text-faint">種別 </dt><dd className="inline">{typeLabel[s.security_type] ?? s.security_type}</dd></div>
          </dl>
        </div>
        <div className="rounded-xl border border-line bg-raised/60 px-5 py-4 md:min-w-[220px]" data-testid="quote">
          <div className="flex items-center justify-between gap-3 text-[11px] text-faint">現在株価 <StatusBadge status={quote.data_status}>{quote.price_state_label ?? undefined}</StatusBadge></div>
          <div className="num mt-2 text-3xl font-semibold">{quote.price === null ? <Dash /> : quote.price.toLocaleString()}<span className="ml-1 text-sm text-faint">{quote.currency}</span></div>
          <div className="mt-1 text-[11px] leading-relaxed text-faint">
            {quote.price !== null && <div className="num text-muted">{fmtDay(quote.as_of)} 終値（基準日）・{quote.provider}（取得日時 {fmtDate(quote.fetched_at)}）</div>}
            <div>{quote.note}</div>
          </div>
        </div>
        <ScoreGauge score={null} />
      </section>

      <nav aria-label="分析の項目" className="sticky top-14 z-20 -mx-4 overflow-x-auto border-y border-line bg-bg/90 px-4 backdrop-blur">
        <ul className="flex gap-1 py-2 text-[13px]">
          <li><a href="#chart" className="block whitespace-nowrap rounded-lg px-3 py-1.5 text-muted hover:bg-raised hover:text-ink">株価チャート</a></li>
          <li><a href="#financials" className="block whitespace-nowrap rounded-lg px-3 py-1.5 text-muted hover:bg-raised hover:text-ink">財務分析</a></li>
          {sections.map((x) => (
            <li key={x.name}><a href={`#${ANCHORS[x.name] ?? x.name}`} className="block whitespace-nowrap rounded-lg px-3 py-1.5 text-muted hover:bg-raised hover:text-ink">{x.name.replace("（100点満点）", "")}</a></li>
          ))}
        </ul>
      </nav>

      <Card id="chart" title="株価チャート" aside={<StatusBadge status={quote.data_status}>{quote.price_state_label ?? undefined}</StatusBadge>}>
        {px.ok && px.data.points.length > 1 ? (
          <LineChart points={px.data.points.map((p) => [p[0], p[1]] as [string, number])} label={`${s.name}の株価`} height={300} width={1400} />
        ) : (
        <div className="grid h-56 place-items-center rounded-xl border border-dashed border-line bg-raised/30 text-center text-sm text-muted sm:h-72">
          <div>
            <div className="text-faint" aria-hidden>
              <svg width="120" height="40" viewBox="0 0 120 40" fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray="3 4"><path d="M2 30 L20 24 L38 28 L56 14 L74 18 L92 8 L118 12" /></svg>
            </div>
            <p className="mt-2">{CHART_EMPTY[quote.price_state ?? ""] ?? "株価データがないため、チャートは表示しません。"}</p>
            <p className="text-[11px] text-faint">{quote.price_state === "not_configured"
              ? (s.market === "JP" ? "J-Quants の APIキーを設定すると表示されます。" : "米国株の株価API（Tiingo / Massive）を設定すると表示されます。")
              : quote.note}</p>
          </div>
        </div>
        )}
      </Card>

      <div className="grid gap-4 md:grid-cols-2">
        <FinancialsCard f={fin.ok ? fin.data : null} />
        {sections.map((x) => (
          <Card key={x.name} id={ANCHORS[x.name]} title={x.name} aside={<StatusBadge status={x.data_status} />}>
            <Placeholder phase={x.available_in_phase}>{x.note}</Placeholder>
          </Card>
        ))}
        {!secs.ok && <ErrorNotice message={secs.error} />}
      </div>

      <footer className="rounded-xl border border-line bg-surface/60 p-4 text-[11px] leading-relaxed text-faint">
        <div>銘柄情報の出所：{s.source}（{fmtDay(s.source_as_of)}時点）</div>
        {fin.ok && fin.data.latest_filed && <div>財務データの出所：{fin.data.source}（最新の提出 {fmtDay(fin.data.latest_filed)}）</div>}
        <div>分析の実行日時：未実行（採点・予測はフェーズ4・5で実装）</div>
        <div>この画面は情報提供を目的としたものであり、投資助言ではありません。</div>
      </footer>
    </div>
  );
}
