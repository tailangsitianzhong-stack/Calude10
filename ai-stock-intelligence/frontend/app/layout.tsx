import type { Metadata, Viewport } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { MobileNav, Nav } from "@/components/Nav";
import { StatusBar } from "@/components/StatusBar";
import { ThemeToggle } from "@/components/ThemeToggle";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "AI STOCK INTELLIGENCE", template: "%s ｜ AI STOCK INTELLIGENCE" },
  description: "日本株・米国株の分析・評価・予測検証プラットフォーム（開発版）",
};
export const viewport: Viewport = { themeColor: "#090c12", width: "device-width", initialScale: 1 };

// 初回描画の前に保存済みの表示モードを適用する（ちらつき防止）
const themeScript = `try{var t=localStorage.getItem('asi-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ja" data-theme="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="grid-bg min-h-screen pb-16 md:pb-0">
        <header className="sticky top-0 z-30 border-b border-line bg-bg/85 backdrop-blur">
          <div className="mx-auto flex h-14 max-w-7xl items-center gap-4 px-4">
            <Link href="/" className="flex items-center gap-2.5" aria-label="AI STOCK INTELLIGENCE ホーム">
              <span className="grid h-8 w-8 place-items-center rounded-lg bg-gradient-to-br from-accent to-up text-[11px] font-black tracking-tight text-bg">AI</span>
              <span className="leading-tight">
                <span className="block text-[13px] font-bold tracking-[0.12em] text-ink">AI STOCK INTELLIGENCE</span>
                <span className="block text-[10px] tracking-wider text-faint">株式分析・評価・予測検証</span>
              </span>
            </Link>
            <div className="ml-auto flex items-center gap-2">
              <Nav />
              <ThemeToggle />
            </div>
          </div>
        </header>
        <MobileNav />
        <StatusBar />
        <main className="mx-auto max-w-7xl px-4 py-6 sm:py-8">{children}</main>
        <footer className="mx-auto max-w-7xl px-4 pb-10 pt-4 text-[11px] leading-relaxed text-faint">
          本サービスは情報提供・学習を目的とした分析ツールであり、投資助言ではありません。投資判断はご自身の責任で行ってください。
          表示する数値には、取得元・取得日時・遅延の有無を併記します。データが取得できない場合は値を表示しません。
        </footer>
      </body>
    </html>
  );
}
