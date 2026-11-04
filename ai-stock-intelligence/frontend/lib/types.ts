export type DataStatus = "ok" | "stale" | "not_connected" | "insufficient" | "error" | "not_implemented";

export interface Provider {
  key: string; name: string; datasets: string[]; configured: boolean; status: DataStatus;
  message: string; missing_settings: string[]; url: string;
}
export interface Dataset { dataset: string; source: string; status: string; rows: number; source_as_of: string | null; finished_at: string | null }
export interface SystemStatus { app_version: string; phase: string; providers: Provider[]; datasets: Dataset[]; generated_at: string }
export interface IndexQuote {
  key: string; name: string; market: string; value: number | null; change_pct: number | null; as_of: string | null;
  provider: string | null; is_delayed: boolean | null; data_status: DataStatus; note: string;
}
export interface Security {
  market: "JP" | "US"; code: string; name: string; exchange: string | null; sector: string | null;
  size_class: string | null; security_type: string; is_active: boolean; source: string; source_as_of: string | null;
}
export interface Quote { price: number | null; currency: string; as_of: string | null; provider: string | null; is_delayed: boolean | null; data_status: DataStatus; note: string }
export interface SecurityDetail { security: Security; quote: Quote }
export interface SearchResult { query: string; market: string | null; total: number; items: Security[] }
export interface Section { name: string; data_status: DataStatus; available_in_phase: number | null; note: string }
