export type DataStatus = "ok" | "stale" | "not_connected" | "insufficient" | "error" | "not_implemented" | "unknown";

export interface Provider {
  key: string; name: string; datasets: string[]; configured: boolean; status: DataStatus;
  message: string; missing_settings: string[]; url: string;
}
export interface Dataset { dataset: string; source: string; status: string; rows: number; source_as_of: string | null; finished_at: string | null }
export interface DataSource {
  dataset: string; name: string; provider: string; reliability: string; delay: string | null; status: DataStatus;
  configured: boolean; last_run_at: string | null; last_run_status: string | null; last_success_at: string | null;
  latest_data_date: string | null; rows: number; message: string | null; quality_issues: number; warnings: string[];
  // 株価のデータセットだけ（backend/app/services/price_status.py の7つの状態）
  price_state?: PriceState | null; price_state_label?: string | null; target_count?: number | null;
  fetched_count?: number | null; used_provider?: string | null;
  price_partial?: boolean | null; price_freshness?: "fresh" | "stale" | null; price_fallback?: boolean | null; target_label?: string | null;
}
/** 株価の接続状態：APIキー未設定／設定済み・未取得／取得済み／取得失敗（API障害）／古いデータ／一部取得／代替の取得元で取得 */
export type PriceState = "not_configured" | "configured_not_fetched" | "ok" | "provider_error" | "stale" | "partial" | "fallback";
export interface SystemStatus { app_version: string; phase: string; providers: Provider[]; datasets: Dataset[]; data_sources: DataSource[]; generated_at: string }
export interface MacroItem {
  key: string; name: string; category: string; unit?: string; frequency?: string; source?: string; reliability?: string; note?: string | null;
  value?: number; obs_date?: string; fetched_at?: string; age_days?: number; data_status: DataStatus;
  prev_1m?: number | null; prev_1y?: number | null; yoy_pct?: number; outlier_flag?: boolean;
}
export interface History { key: string; name: string; points: [string, number][] }
export interface FinRow { period_end: string; values: Record<string, number>; filed: string | null; tags?: Record<string, string>; accounting_standard?: string }
export interface Financials {
  security: Security; as_of: string | null; source: string | null; reliability: string | null; labels: Record<string, string>;
  note: string | null; quality_issues: { type: string; detail: Record<string, unknown> }[]; data_status: DataStatus;
  annual: FinRow[]; ttm: (FinRow & { basis: string }) | null; balance: FinRow | null; latest_filed: string | null;
  currency: string | null; other_currencies: string[]; tag_notes: { concept: string; tag: string; note: string }[];
  item_notes?: string[]; delay_note?: string | null;  // 日本企業（J-Quants）のときだけ
  freshness?: "fresh" | "stale" | "unknown"; freshness_days?: number | null; fetched_at?: string | null;
}
export interface IndexQuote {
  key: string; name: string; market: string; value: number | null; change_pct: number | null; as_of: string | null;
  provider: string | null; is_delayed: boolean | null; data_status: DataStatus; note: string;
}
export interface Security {
  market: "JP" | "US"; code: string; cik?: number | null; name: string; exchange: string | null; sector: string | null;
  size_class: string | null; security_type: string; is_active: boolean; source: string; source_as_of: string | null;
}
export interface Quote {
  price: number | null; currency: string; as_of: string | null; fetched_at?: string | null; provider: string | null;
  is_delayed: boolean | null; data_status: DataStatus; note: string;
  price_state?: PriceState | null; price_state_label?: string | null;  // as_of は株価の基準日、fetched_at は取得日時
}
export interface SecurityDetail { security: Security; quote: Quote }
export interface SearchItem extends Security { price_state?: PriceState | null; price_state_label?: string | null; price_data_status?: DataStatus | null }
export interface SearchResult { query: string; market: string | null; total: number; items: SearchItem[] }
export interface Section { name: string; data_status: DataStatus; available_in_phase: number | null; note: string }
