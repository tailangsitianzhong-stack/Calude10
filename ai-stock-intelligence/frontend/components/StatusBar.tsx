import Link from "next/link";
import { api } from "@/lib/api";
import { fmtDay } from "@/lib/format";
import type { SystemStatus } from "@/lib/types";
import { StatusBadge } from "./ui";

const SHORT: Record<string, string> = {
  fred: "金利・為替・指数", sec_financials: "米国企業の財務", prices_jp: "日本株の株価", prices_us: "米国株の株価",
};

// 全ページの上部：データの取得状況（基準日・遅延）を常に表示する（設計書 11章）
export async function StatusBar() {
  const r = await api<SystemStatus>("/api/v1/system/status");
  if (!r.ok) {
    return (
      <div className="border-b border-down/30 bg-down/10 px-4 py-2 text-xs text-down" role="status" aria-label="データの状態">
        データの状態を取得できません：{r.error}
      </div>
    );
  }
  const items = r.data.data_sources.filter((d) => d.dataset in SHORT);
  const warn = r.data.data_sources.flatMap((d) => d.warnings);
  return (
    <div className="border-b border-line bg-surface/60 px-4 py-2 text-[12px] text-muted" role="status" aria-label="データの状態">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-4 gap-y-1.5">
        {items.map((d) => (
          <span key={d.dataset} className="flex items-center gap-1.5" data-testid={`bar-${d.dataset}`}>
            {SHORT[d.dataset]} <StatusBadge status={d.status} />
            {d.latest_data_date && <span className="num text-faint">{fmtDay(d.latest_data_date)}まで</span>}
          </span>
        ))}
        <Link href="/data" className="ml-auto text-accent hover:underline">データの詳細</Link>
        {warn.map((w) => <span key={w} className="basis-full text-warn">⚠ {w}</span>)}
      </div>
    </div>
  );
}
