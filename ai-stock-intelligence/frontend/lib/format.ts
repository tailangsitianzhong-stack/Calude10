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

// 通貨コード（ISO 4217）の表示名。一覧にない通貨はコードのまま表示する
const CURRENCY_NAMES: Record<string, string> = {
  USD: "ドル", CAD: "カナダドル", EUR: "ユーロ", GBP: "英ポンド", JPY: "円", CHF: "スイスフラン", AUD: "豪ドル",
  CNY: "人民元", HKD: "香港ドル", TWD: "台湾ドル", KRW: "ウォン", INR: "インドルピー", BRL: "レアル", MXN: "メキシコペソ",
  DKK: "デンマーククローネ", SEK: "スウェーデンクローナ", NOK: "ノルウェークローネ", ILS: "新シェケル",
};
export const currencyName = (cur: string | null | undefined) => (cur ? CURRENCY_NAMES[cur] ?? ` ${cur}` : "");

// 財務の金額を、報告通貨の「億」「兆」で表示（例 1.23兆ドル、456億カナダドル）
export function fmtMoney(v: number | null | undefined, cur: string | null | undefined = "USD"): string {
  if (v === null || v === undefined) return "—";
  const u = currencyName(cur ?? "USD");
  const a = Math.abs(v);
  if (a >= 1e12) return `${(v / 1e12).toFixed(2)}兆${u}`;
  if (a >= 1e8) return `${(v / 1e8).toLocaleString("ja-JP", { maximumFractionDigits: 1 })}億${u}`;
  if (a >= 1e4) return `${(v / 1e4).toLocaleString("ja-JP", { maximumFractionDigits: 0 })}万${u}`;
  return `${v.toLocaleString("ja-JP")}${u}`;
}

// 1株あたりの値（EPSなど）：ドルは「$1.23」、それ以外は「1.23カナダドル」
export function fmtPerShare(v: number | null | undefined, cur: string | null | undefined = "USD"): string {
  if (v === null || v === undefined) return "—";
  return !cur || cur === "USD" ? `$${v.toFixed(2)}` : `${v.toFixed(2)}${currencyName(cur)}`;
}

export function signed(v: number | null | undefined, digits = 2, suffix = "%"): { text: string; dir: "up" | "down" | "flat" } {
  if (v === null || v === undefined || Number.isNaN(v)) return { text: "—", dir: "flat" };
  const dir = v > 0 ? "up" : v < 0 ? "down" : "flat";
  return { text: `${dir === "up" ? "▲" : dir === "down" ? "▼" : ""}${Math.abs(v).toFixed(digits)}${suffix}`, dir };
}
