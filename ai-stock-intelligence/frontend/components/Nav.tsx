"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const ITEMS = [
  { href: "/", label: "ダッシュボード", short: "ホーム" },
  { href: "/search", label: "銘柄検索", short: "検索" },
  { href: "/rankings", label: "AIランキング", short: "ランキング" },
  { href: "/validation", label: "予測検証", short: "検証" },
];

function useActive() {
  const path = usePathname();
  return (h: string) => (h === "/" ? path === "/" : path.startsWith(h));
}

export function Nav() {
  const active = useActive();
  return (
    <nav aria-label="メイン" className="hidden items-center gap-1 md:flex">
        {ITEMS.map((i) => (
          <Link key={i.href} href={i.href} aria-current={active(i.href) ? "page" : undefined}
            className={`rounded-lg px-3 py-2 text-[13px] font-medium transition ${active(i.href) ? "bg-raised text-ink" : "text-muted hover:text-ink"}`}>
            {i.label}
          </Link>
        ))}
    </nav>
  );
}

// スマートフォン：画面下部のタブバー。
// 注意：backdrop-filter を持つ要素（ヘッダー）の中に置くと、position: fixed の基準がその要素になり上部に表示されてしまう。
// そのため、必ず body の直下（layout）に置く。
export function MobileNav() {
  const active = useActive();
  return (
    <nav aria-label="メイン（モバイル）" className="fixed inset-x-0 bottom-0 z-40 grid grid-cols-4 border-t border-line bg-surface/95 backdrop-blur md:hidden">
        {ITEMS.map((i) => (
          <Link key={i.href} href={i.href} aria-current={active(i.href) ? "page" : undefined}
            className={`py-3 text-center text-[12px] font-medium ${active(i.href) ? "text-accent" : "text-muted"}`}>
            {i.short}
          </Link>
        ))}
    </nav>
  );
}
