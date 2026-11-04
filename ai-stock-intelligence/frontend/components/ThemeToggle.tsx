"use client";

import { useEffect, useState } from "react";

// 表示モードの選択はこの端末だけの好みとして保存する（失敗しても動作に影響しない）
export function ThemeToggle() {
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  useEffect(() => {
    try {
      const t = localStorage.getItem("asi-theme");
      if (t === "light" || t === "dark") setTheme(t);
    } catch {}
  }, []);
  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("asi-theme", next); } catch {}
  };
  return (
    <button type="button" onClick={toggle} aria-label={theme === "dark" ? "ライトモードに切り替え" : "ダークモードに切り替え"}
      className="grid h-9 w-9 place-items-center rounded-lg border border-line bg-raised text-muted transition hover:text-ink">
      {theme === "dark" ? (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
      ) : (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg>
      )}
    </button>
  );
}
