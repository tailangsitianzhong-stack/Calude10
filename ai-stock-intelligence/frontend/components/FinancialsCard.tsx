import { fmtDay, fmtUsd } from "@/lib/format";
import type { Financials } from "@/lib/types";
import { Card, Placeholder, StatusBadge } from "./ui";

const ROWS: [string, "usd" | "eps"][] = [
  ["revenue", "usd"], ["operating_income", "usd"], ["net_income", "usd"], ["eps_diluted", "eps"],
  ["cfo", "usd"], ["capex", "usd"], ["dividends_paid", "usd"], ["buybacks", "usd"],
];

const fmt = (v: number | undefined, kind: "usd" | "eps") =>
  v === undefined ? "—" : kind === "eps" ? `$${v.toFixed(2)}` : fmtUsd(v);

const margin = (r: Record<string, number>) =>
  r.revenue && r.operating_income !== undefined ? ((r.operating_income / r.revenue) * 100).toFixed(1) + "%" : "—";

export function FinancialsCard({ f }: { f: Financials | null }) {
  if (!f || f.annual.length === 0) {
    return (
      <Card id="financials" title="財務分析" aside={<StatusBadge status={f?.data_status ?? "not_connected"} />}>
        <Placeholder>{f?.note ?? "財務データを取得できませんでした。"}</Placeholder>
      </Card>
    );
  }
  const years = f.annual.slice(-5);
  return (
    <Card id="financials" title="財務分析" className="md:col-span-2"
      aside={<span className="flex items-center gap-2"><span className="hidden text-[11px] text-faint sm:inline">{f.source}・{f.reliability}</span><StatusBadge status={f.data_status} /></span>}>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] text-right text-[13px]" data-testid="financials-table">
          <thead className="text-[11px] text-faint">
            <tr>
              <th className="pb-2 text-left font-medium">項目</th>
              {years.map((y) => <th key={y.period_end} className="num pb-2 font-medium">{y.period_end.slice(0, 7)}期</th>)}
              {f.ttm && <th className="num pb-2 font-medium text-accent">直近4四半期</th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-line/60">
            {ROWS.map(([k, kind]) => (
              <tr key={k}>
                <td className="py-2 text-left text-muted">{f.labels[k] ?? k}</td>
                {years.map((y) => <td key={y.period_end} className="num py-2">{fmt(y.values[k], kind)}</td>)}
                {f.ttm && <td className="num py-2 text-accent">{fmt(f.ttm.values[k], kind)}</td>}
              </tr>
            ))}
            <tr>
              <td className="py-2 text-left text-muted">営業利益率</td>
              {years.map((y) => <td key={y.period_end} className="num py-2">{margin(y.values)}</td>)}
              {f.ttm && <td className="num py-2 text-accent">{margin(f.ttm.values)}</td>}
            </tr>
            <tr className="text-[11px] text-faint">
              <td className="py-2 text-left">提出日</td>
              {years.map((y) => <td key={y.period_end} className="num py-2">{fmtDay(y.filed)}</td>)}
              {f.ttm && <td className="num py-2">{fmtDay(f.ttm.filed)}</td>}
            </tr>
          </tbody>
        </table>
      </div>
      <div className="mt-4 grid gap-3 text-[12px] text-muted sm:grid-cols-2">
        {f.balance && (
          <div className="rounded-xl border border-line bg-raised/40 p-3">
            <div className="mb-1 text-[11px] font-semibold text-faint">貸借対照表（{fmtDay(f.balance.period_end)}時点、{fmtDay(f.balance.filed)}提出）</div>
            <div className="num grid grid-cols-2 gap-x-3 gap-y-0.5">
              <span>総資産</span><span className="text-right text-ink">{fmtUsd(f.balance.values.assets)}</span>
              <span>株主資本</span><span className="text-right text-ink">{fmtUsd(f.balance.values.equity)}</span>
              <span>現金</span><span className="text-right text-ink">{fmtUsd(f.balance.values.cash)}</span>
              <span>長期借入金</span><span className="text-right text-ink">{fmtUsd(f.balance.values.debt_noncurrent)}</span>
            </div>
          </div>
        )}
        <div className="rounded-xl border border-line bg-raised/40 p-3 leading-relaxed">
          <div className="mb-1 text-[11px] font-semibold text-faint">データについて</div>
          <div>出所：{f.source}（信頼性：{f.reliability}）</div>
          {f.ttm && <div>直近4四半期の計算：{f.ttm.basis}</div>}
          <div>最新の提出：{fmtDay(f.latest_filed)}。値は会計上（GAAP）の数値で、一時的な損益を含みます。</div>
          {f.quality_issues.length > 0 && (
            <div className="mt-1 text-warn">
              ⚠ 品質の注意：{f.quality_issues.map((q) => (q.type === "stale" ? "最新の提出から120日超（SEC側のデータが更新されていない可能性）" : q.type)).join("、")}
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}
