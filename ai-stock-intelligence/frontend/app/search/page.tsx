import Link from "next/link";
import { SearchBox } from "@/components/SearchBox";
import { Card, ErrorNotice, StatusBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDay, marketLabel } from "@/lib/format";
import type { SearchResult } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata = { title: "銘柄検索" };

export default async function SearchPage({ searchParams }: { searchParams: Promise<{ q?: string; market?: string }> }) {
  const { q = "", market = "" } = await searchParams;
  const m = market === "JP" || market === "US" ? market : "";
  const params = new URLSearchParams({ query: q.slice(0, 64), limit: "50" });
  if (m) params.set("market", m);
  const r = q.trim() ? await api<SearchResult>(`/api/v1/securities?${params}`) : null;
  return (
    <div className="space-y-5">
      <div>
        <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-accent">Search</p>
        <h1 className="mt-1 text-2xl font-bold tracking-tight">銘柄検索</h1>
        <p className="mt-1 text-sm text-muted">銘柄名・ティッカー・証券コードで検索できます（全角・半角、大文字・小文字は区別しません）。ETF・REITは除きます。</p>
      </div>
      <SearchBox query={q} market={m} />
      {r === null ? (
        <p className="text-sm text-faint">検索語を入力してください。</p>
      ) : !r.ok ? (
        <ErrorNotice message={r.error} />
      ) : (
        <Card title={<>検索結果 <span className="num ml-1 text-muted">{r.data.total.toLocaleString()}件</span>{r.data.total > r.data.items.length && <span className="ml-1 text-[11px] font-normal text-faint">（上位{r.data.items.length}件を表示）</span>}</>}>
          {r.data.items.length === 0 ? (
            <p className="text-sm text-muted" data-testid="no-results">該当する銘柄がありません。</p>
          ) : (
            <ul className="divide-y divide-line/70" data-testid="results">
              {r.data.items.map((s) => (
                <li key={`${s.market}-${s.code}`}>
                  <Link href={`/stocks/${s.market}/${encodeURIComponent(s.code)}`}
                    className="-mx-2 flex items-center gap-3 rounded-lg px-2 py-3 transition hover:bg-raised">
                    <span className={`num w-16 shrink-0 rounded-md px-2 py-1 text-center text-xs font-semibold ${s.market === "JP" ? "bg-accent/15 text-accent" : "bg-up/15 text-up"}`}>{s.code}</span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{s.name}</span>
                      <span className="block truncate text-[11px] text-faint">{marketLabel(s.market)}・{s.exchange ?? "—"}{s.sector ? `・${s.sector}` : ""}{s.size_class ? `・${s.size_class}` : ""}</span>
                    </span>
                    <span className="hidden sm:block"><StatusBadge status={s.price_data_status ?? "not_connected"}>株価 {s.price_state_label ?? "状態不明"}</StatusBadge></span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-4 text-[11px] text-faint">銘柄マスタの出所：{r.data.items[0]?.source ?? "—"}（{fmtDay(r.data.items[0]?.source_as_of)}時点）など</p>
        </Card>
      )}
    </div>
  );
}
