export type DataStatus = "ok" | "stale" | "not_connected" | "insufficient" | "error" | "not_implemented";

export interface Provider {
  key: string; name: string; datasets: string[]; configured: boolean; status: DataStatus;
  message: string; missing_settings: string[]; url: string;
}
export interface Dataset { dataset: string; source: string; status: string; rows: number; source_as_of: string | null; finished_at: string | null }
export interface DataSource {
  dataset: string; name: string; provider: string; reliability: string; delay: string | null; status: DataStatus;
  configured: boolean; last_run_at: string | null; last_run_status: string | null; last_success_at: string | null;
  latest_data_date: string | null; rows: number; message: string | null; quality_issues: number; warnings: string[];
}
export interface SystemStatus { app_version: string; phase: string; providers: Provider[]; datasets: Dataset[]; data_sources: DataSource[]; generated_at: string }
export interface MacroItem {
  key: string; name: string; category: string; unit?: string; frequency?: string; source?: string; reliability?: string; note?: string | null;
  value?: number; obs_date?: string; fetched_at?: string; age_days?: number; data_status: DataStatus;
  prev_1m?: number | null; prev_1y?: number | null; yoy_pct?: number; outlier_flag?: boolean;
}
export interface History { key: string; name: string; points: [string, number][] }
export interface FinRow { period_end: string; values: Record<string, number>; filed: string | null; tags?: Record<string, string> }
export interface Financials {
  security: Security; as_of: string | null; source: string | null; reliability: string | null; labels: Record<string, string>;
  note: string | null; quality_issues: { type: string; detail: Record<string, unknown> }[]; data_status: DataStatus;
  annual: FinRow[]; ttm: (FinRow & { basis: string }) | null; balance: FinRow | null; latest_filed: string | null;
  currency: string | null; other_currencies: string[]; tag_notes: { concept: string; tag: string; note: string }[];
}
export interface IndexQuote {
  key: string; name: string; market: string; value: number | null; change_pct: number | null; as_of: string | null;
  provider: string | null; is_delayed: boolean | null; data_status: DataStatus; note: string;
}
export interface Security {
  market: "JP" | "US"; code: string; cik?: number | null; name: string; exchange: string | null; sector: string | null;
  size_class: string | null; security_type: string; is_active: boolean; source: string; source_as_of: string | null;
}
export interface Quote { price: number | null; currency: string; as_of: string | null; fetched_at?: string | null; provider: string | null; is_delayed: boolean | null; data_status: DataStatus; note: string }
export interface SecurityDetail { security: Security; quote: Quote }
export interface SearchResult { query: string; market: string | null; total: number; items: Security[] }
export interface Section { name: string; data_status: DataStatus; available_in_phase: number | null; note: string }
