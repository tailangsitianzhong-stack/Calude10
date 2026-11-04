export const marketLabel = (m: string) => (m === "JP" ? "日本株" : m === "US" ? "米国株" : m);

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function fmtDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replaceAll("-", "/");
}

export const typeLabel: Record<string, string> = { common: "株式", etf: "ETF・ETN", reit: "REIT・ファンド", other: "その他" };
