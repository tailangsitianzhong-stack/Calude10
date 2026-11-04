export function SearchBox({ query = "", market = "", size = "md" }: { query?: string; market?: string; size?: "md" | "lg" }) {
  const big = size === "lg";
  return (
    <form action="/search" method="get" role="search" className="flex w-full flex-col gap-2 sm:flex-row">
      <label className="sr-only" htmlFor={`q-${size}`}>銘柄名・ティッカー・証券コード</label>
      <div className="relative flex-1">
        <svg className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
        <input id={`q-${size}`} name="q" defaultValue={query} autoComplete="off" placeholder="銘柄名・ティッカー・証券コード（例：トヨタ、7203、AAPL）"
          className={`w-full rounded-xl border border-line bg-raised pl-9 pr-3 text-ink placeholder:text-faint focus:border-accent focus:outline-none ${big ? "h-12 text-[15px]" : "h-10 text-sm"}`} />
      </div>
      <div className="flex gap-2">
        <label className="sr-only" htmlFor={`m-${size}`}>市場</label>
        <select id={`m-${size}`} name="market" defaultValue={market}
          className={`rounded-xl border border-line bg-raised px-3 text-sm text-ink focus:border-accent focus:outline-none ${big ? "h-12" : "h-10"}`}>
          <option value="">すべての市場</option>
          <option value="JP">日本株</option>
          <option value="US">米国株</option>
        </select>
        <button type="submit" className={`rounded-xl bg-accent px-5 text-sm font-semibold text-bg transition hover:brightness-110 ${big ? "h-12" : "h-10"}`}>
          検索
        </button>
      </div>
    </form>
  );
}
