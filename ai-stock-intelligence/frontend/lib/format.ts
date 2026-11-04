export const marketLabel = (m: string) => (m === "JP" ? "日本株" : m === "US" ? "米国株" : m);

// API の日時はUTC。タイムゾーンの表記がない値（SQLite）もUTCとして扱い、日本時間で表示する
export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) || iso.length <= 10 ? iso : iso + "Z");
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function fmtDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replaceAll("-", "/");
}

export const typeLabel: Record<string, string> = { common: "株式", etf: "ETF・ETN", reit: "REIT・ファンド", other: "その他" };

export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("ja-JP", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// 財務の金額（ドル）を「億ドル」「兆ドル」で表示
export function fmtUsd(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const a = Math.abs(v);
  if (a >= 1e12) return `${(v / 1e12).toFixed(2)}兆ドル`;
  if (a >= 1e8) return `${(v / 1e8).toLocaleString("ja-JP", { maximumFractionDigits: 1 })}億ドル`;
  if (a >= 1e4) return `${(v / 1e4).toLocaleString("ja-JP", { maximumFractionDigits: 0 })}万ドル`;
  return v.toLocaleString("ja-JP");
}

export function signed(v: number | null | undefined, digits = 2, suffix = "%"): { text: string; dir: "up" | "down" | "flat" } {
  if (v === null || v === undefined || Number.isNaN(v)) return { text: "—", dir: "flat" };
  const dir = v > 0 ? "up" : v < 0 ? "down" : "flat";
  return { text: `${dir === "up" ? "▲" : dir === "down" ? "▼" : ""}${Math.abs(v).toFixed(digits)}${suffix}`, dir };
}
