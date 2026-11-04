import { currencyName, fmtDay, fmtMoney, fmtPerShare } from "@/lib/format";
import type { Financials } from "@/lib/types";
import { Card, Placeholder, StatusBadge } from "./ui";

const ROWS: [string, "money" | "eps"][] = [
  ["revenue", "money"], ["operating_income", "money"], ["net_income", "money"], ["eps_diluted", "eps"],
  ["cfo", "money"], ["capex", "money"], ["dividends_paid", "money"], ["buybacks", "money"],
];
// 日本企業（J-Quants の決算短信のサマリー）の項目。経常利益は日本基準だけの項目
const ROWS_JP: [string, "money" | "eps"][] = [
  ["revenue", "money"], ["operating_income", "money"], ["ordinary_income", "money"], ["net_income", "money"],
  ["eps_basic", "eps"], ["eps_diluted", "eps"], ["cfo", "money"], ["cfi", "money"], ["cff", "money"],
];
const STD_NAME: Record<string, string> = { JGAAP: "日本基準", IFRS: "IFRS", USGAAP: "米国基準", JMIS: "JMIS", Foreign: "外国会社", REIT: "REIT" };
const stdName = (s?: string) => (s ? s.split("・").map((x) => STD_NAME[x] ?? x).join("・") : "—");

// 報告されていない値は「—」（推測で埋めない）
const fmt = (v: number | undefined, kind: "money" | "eps", cur: string | null) =>
  v === undefined ? "—" : kind === "eps" ? fmtPerShare(v, cur) : fmtMoney(v, cur);

const ISSUE_TEXT: Record<string, string> = {
  stale: "最新の提出から120日超",
  gap_filled: "SECの集計API（companyfacts）に未反映の提出書類を、書類のXBRLから直接取得",
  missing: "SECに提出済みの書類の一部を読めなかった",
  float_anomaly: "企業が提出した公開流通時価総額の桁に誤りの可能性（規模の順位づけから除外）",
  invalid_value: "数値に変換できない値があったため、その項目を保存していない",
  unknown_doc_type: "公式の一覧にない開示書類種別",
};
const issueText = (q: { type: string; detail: Record<string, unknown> }) =>
  (ISSUE_TEXT[q.type] ?? q.type) + (q.type === "stale" && typeof q.detail?.note === "string" ? `（${q.detail.note}）` : "");

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
  const cur = f.currency;
  const jp = f.security.market === "JP";
  const rows = jp ? ROWS_JP : ROWS;
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
            {jp && (
              <tr className="text-[11px] text-faint" data-testid="accounting-standard">
                <td className="py-2 text-left">会計基準</td>
                {years.map((y) => <td key={y.period_end} className="py-2">{stdName(y.accounting_standard)}</td>)}
                {f.ttm && <td className="py-2" />}
              </tr>
            )}
            {rows.map(([k, kind]) => (
              <tr key={k}>
                <td className="py-2 text-left text-muted">{f.labels[k] ?? k}</td>
                {years.map((y) => <td key={y.period_end} className="num py-2">{fmt(y.values[k], kind, cur)}</td>)}
                {f.ttm && <td className="num py-2 text-accent">{fmt(f.ttm.values[k], kind, cur)}</td>}
              </tr>
            ))}
            <tr>
              <td className="py-2 text-left text-muted">営業利益率</td>
              {years.map((y) => <td key={y.period_end} className="num py-2">{margin(y.values)}</td>)}
              {f.ttm && <td className="num py-2 text-accent">{margin(f.ttm.values)}</td>}
            </tr>
            <tr className="text-[11px] text-faint">
              <td className="py-2 text-left">{jp ? "開示日" : "提出日"}</td>
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
            {jp ? (
            <div className="num grid grid-cols-2 gap-x-3 gap-y-0.5" data-testid="balance-table">
              <span>総資産</span><span className="text-right text-ink">{fmtMoney(f.balance.values.assets, cur)}</span>
              <span>{f.labels.net_assets ?? "純資産"}</span><span className="text-right text-ink">{fmtMoney(f.balance.values.net_assets, cur)}</span>
              <span>自己資本比率</span><span className="text-right text-ink">{f.balance.values.equity_ratio === undefined ? "—" : (f.balance.values.equity_ratio * 100).toFixed(1) + "%"}</span>
              <span>現金及び現金同等物</span><span className="text-right text-ink">{fmtMoney(f.balance.values.cash, cur)}</span>
              <span>負債</span><span className="text-right text-ink">{f.balance.values.liabilities === undefined ? "—（未取得）" : fmtMoney(f.balance.values.liabilities, cur)}</span>
            </div>
            ) : (
            <div className="num grid grid-cols-2 gap-x-3 gap-y-0.5" data-testid="balance-table">
              <span>総資産</span><span className="text-right text-ink">{fmtMoney(f.balance.values.assets, cur)}</span>
              <span>負債</span><span className="text-right text-ink">{fmtMoney(f.balance.values.liabilities, cur)}</span>
              <span>株主資本</span><span className="text-right text-ink">{fmtMoney(f.balance.values.equity, cur)}</span>
              <span>現金</span><span className="text-right text-ink">{fmtMoney(f.balance.values.cash, cur)}</span>
              <span>長期借入金</span><span className="text-right text-ink">{fmtMoney(f.balance.values.debt_noncurrent, cur)}</span>
            </div>
            )}
          </div>
        )}
        <div className="rounded-xl border border-line bg-raised/40 p-3 leading-relaxed">
          <div className="mb-1 text-[11px] font-semibold text-faint">データについて</div>
          <div>出所：{f.source}（信頼性：{f.reliability}）</div>
          {f.delay_note && <div className="text-warn" data-testid="financials-delay">⚠ {f.delay_note}</div>}
          {(f.item_notes ?? []).map((n) => <div key={n}>※ {n}</div>)}
          {cur && (
            <div data-testid="financials-currency">
              報告通貨：{cur}（{currencyName(cur).trim()}）。金額は企業が報告した通貨のままで、換算していません。
              {f.other_currencies.length > 0 && `（${f.other_currencies.join("・")}建ての値は除外）`}
            </div>
          )}
          {f.tag_notes.map((n) => <div key={n.concept}>※ {f.labels[n.concept] ?? n.concept}：{n.note}（XBRLタグ {n.tag}）</div>)}
          <div>「—」は企業がその項目を報告していないことを示します（推測で補完しません）。</div>
          {f.ttm && <div>直近4四半期の計算：{f.ttm.basis}</div>}
          <div>{jp ? `最新の開示：${fmtDay(f.latest_filed)}。値は会計上の数値で、一時的な損益を含みます。`
                   : `最新の提出：${fmtDay(f.latest_filed)}。値は会計上（GAAP）の数値で、一時的な損益を含みます。`}</div>
          {f.quality_issues.length > 0 && (
            <div className="mt-1 text-warn">
              ⚠ 品質の注意：{f.quality_issues.map(issueText).join("、")}
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}
