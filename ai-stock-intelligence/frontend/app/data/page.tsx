import { Card, ErrorNotice, StatusBadge } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDate, fmtDay } from "@/lib/format";
import type { SystemStatus } from "@/lib/types";

export const dynamic = "force-dynamic";
export const metadata = { title: "データの状態" };

export default async function DataPage() {
  const r = await api<SystemStatus>("/api/v1/system/status");
  return (
    <div className="space-y-5">
      <div>
        <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-accent">Data</p>
        <h1 className="mt-1 text-2xl font-bold tracking-tight">データの状態</h1>
        <p className="mt-1 text-sm text-muted">すべてのデータについて、取得元・信頼性・遅延・最終取得日時・最新データの日付・品質の検証結果を表示します。</p>
      </div>
      {!r.ok ? <ErrorNotice message={r.error} /> : (
        <>
          <div className="grid gap-4 md:grid-cols-2">
            {r.data.data_sources.map((d) => (
              <Card key={d.dataset} title={d.name} aside={<StatusBadge status={d.status} />}>
                <dl className="grid grid-cols-[7.5em_1fr] gap-x-3 gap-y-1.5 text-[13px]" data-testid={`ds-${d.dataset}`}>
                  <dt className="text-faint">取得元</dt><dd>{d.provider}</dd>
                  <dt className="text-faint">信頼性</dt><dd>{d.reliability}</dd>
                  <dt className="text-faint">遅延</dt><dd>{d.delay ?? "—"}</dd>
                  <dt className="text-faint">最新データ</dt><dd className="num">{fmtDay(d.latest_data_date)}</dd>
                  <dt className="text-faint">最終取得</dt><dd className="num">{fmtDate(d.last_success_at)}{d.last_run_status && d.last_run_status !== "success" && <span className="ml-2 text-faint">（最終実行：{d.last_run_status}）</span>}</dd>
                  <dt className="text-faint">件数</dt><dd className="num">{d.rows.toLocaleString()}{d.dataset === "sec_financials" ? "社" : d.dataset.startsWith("prices") ? "銘柄" : "件"}</dd>
                  <dt className="text-faint">品質の注意</dt><dd className="num">{d.quality_issues}件</dd>
                </dl>
                {d.warnings.map((w) => <p key={w} className="mt-3 rounded-lg border border-warn/30 bg-warn/10 p-2 text-[12px] text-warn">⚠ {w}</p>)}
                {!d.configured && <p className="mt-3 text-[12px] text-muted">APIキーが未設定です。設定方法は docs/phase3_data_sources.md を参照してください。</p>}
                {d.message && d.configured && <p className="mt-3 text-[11px] text-faint">{d.message}</p>}
              </Card>
            ))}
          </div>
          <Card title="プロバイダーの接続設定">
            <ul className="divide-y divide-line/70 text-[13px]">
              {r.data.providers.map((p) => (
                <li key={p.key} className="flex flex-wrap items-center gap-3 py-2.5">
                  <span className="min-w-[14em] font-medium">{p.name}</span>
                  <StatusBadge status={p.status} />
                  <span className="text-xs text-muted">{p.message}{p.missing_settings.length > 0 && `（未設定：${p.missing_settings.join(", ")}）`}</span>
                </li>
              ))}
            </ul>
            <p className="mt-3 text-[11px] text-faint">APIキーはサーバー側の .env だけに保存され、この画面やブラウザには送られません。</p>
          </Card>
        </>
      )}
    </div>
  );
}
