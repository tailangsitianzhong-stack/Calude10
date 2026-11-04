import { api } from "@/lib/api";
import { fmtDay } from "@/lib/format";
import type { SystemStatus } from "@/lib/types";
import { StatusBadge } from "./ui";

// 全ページの上部：データの取得状況を常に表示する（設計書 11章）
export async function StatusBar() {
  const r = await api<SystemStatus>("/api/v1/system/status");
  if (!r.ok) {
    return (
      <div className="border-b border-down/30 bg-down/10 px-4 py-2 text-xs text-down" role="status">
        データの状態を取得できません：{r.error}
      </div>
    );
  }
  const st = r.data;
  const price = st.providers.filter((p) => p.datasets.some((d) => d.startsWith("prices")));
  const anyPrice = price.some((p) => p.status === "ok");
  const master = Object.fromEntries(st.datasets.map((d) => [d.dataset, d]));
  return (
    <div className="border-b border-line bg-surface/60 px-4 py-2 text-[12px] text-muted" role="status" aria-label="データの状態">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-4 gap-y-1.5">
        <span className="flex items-center gap-2">
          株価データ <StatusBadge status={anyPrice ? "ok" : "not_connected"} />
          {!anyPrice && <span className="text-faint">実データ未取得のため、株価・指数は表示しません</span>}
        </span>
        <span className="hidden h-3 w-px bg-line sm:block" aria-hidden />
        <span>
          銘柄マスタ：日本 <span className="num text-ink">{master.securities_jp?.rows?.toLocaleString() ?? "—"}</span>
          <span className="text-faint">（{fmtDay(master.securities_jp?.source_as_of)}時点）</span>
          ・米国 <span className="num text-ink">{master.securities_us?.rows?.toLocaleString() ?? "—"}</span>
          <span className="text-faint">（{fmtDay(master.securities_us?.source_as_of)}時点）</span>
        </span>
        <span className="ml-auto text-faint">v{st.app_version} ・ {st.phase}</span>
      </div>
    </div>
  );
}
