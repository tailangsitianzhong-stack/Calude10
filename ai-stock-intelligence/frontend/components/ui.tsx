import type { DataStatus } from "@/lib/types";
import type { ReactNode } from "react";

const STATUS: Record<DataStatus, { label: string; cls: string; dot: string }> = {
  ok: { label: "取得済み", cls: "text-up border-up/30 bg-up/10", dot: "bg-up" },
  stale: { label: "古いデータ", cls: "text-warn border-warn/30 bg-warn/10", dot: "bg-warn" },
  not_connected: { label: "未接続", cls: "text-muted border-line bg-raised", dot: "bg-faint" },
  insufficient: { label: "データ不足", cls: "text-warn border-warn/30 bg-warn/10", dot: "bg-warn" },
  error: { label: "エラー", cls: "text-down border-down/30 bg-down/10", dot: "bg-down" },
  not_implemented: { label: "準備中", cls: "text-info border-info/30 bg-info/10", dot: "bg-info" },
  unknown: { label: "鮮度判定不能", cls: "text-muted border-line bg-raised", dot: "bg-faint" },
};

export function StatusBadge({ status, children }: { status: DataStatus; children?: ReactNode }) {
  const s = STATUS[status];
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-medium ${s.cls}`}
      data-status={status}>
      <span className={`h-1.5 w-1.5 rounded-full ${s.dot}`} aria-hidden />
      {children ?? s.label}
    </span>
  );
}

export function Card({ title, aside, children, className = "", id }: { title?: ReactNode; aside?: ReactNode; children: ReactNode; className?: string; id?: string }) {
  return (
    <section id={id} className={`min-w-0 rounded-card border border-line bg-surface/80 shadow-[0_1px_0_rgb(255_255_255/0.03)_inset] ${className}`}>
      {(title || aside) && (
        <header className="flex items-center justify-between gap-3 border-b border-line/70 px-4 py-3 sm:px-5">
          <h2 className="text-[13px] font-semibold tracking-wide text-ink">{title}</h2>
          {aside}
        </header>
      )}
      <div className="p-4 sm:p-5">{children}</div>
    </section>
  );
}

export function Placeholder({ phase, children }: { phase?: number | null; children: ReactNode }) {
  return (
    <div className="flex items-start gap-3 rounded-xl border border-dashed border-line bg-raised/40 p-4 text-sm text-muted">
      <span className="mt-0.5 text-faint" aria-hidden>◇</span>
      <div className="space-y-1">
        {phase ? <div className="text-[11px] font-semibold uppercase tracking-wider text-info">フェーズ{phase}で実装予定</div> : null}
        <div>{children}</div>
      </div>
    </div>
  );
}

export function ErrorNotice({ message }: { message: string }) {
  return (
    <div role="alert" className="rounded-xl border border-down/40 bg-down/10 p-4 text-sm text-down">{message}</div>
  );
}

export function Dash() {
  return <span className="text-faint" aria-label="値なし">—</span>;
}
