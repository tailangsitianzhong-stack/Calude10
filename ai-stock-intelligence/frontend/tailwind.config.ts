import type { Config } from "tailwindcss";

// 色はすべて CSS 変数（app/globals.css）で定義し、ダーク・ライトで切り替える
const v = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: v("bg"),
        surface: v("surface"),
        raised: v("raised"),
        line: v("line"),
        ink: v("ink"),
        muted: v("muted"),
        faint: v("faint"),
        accent: v("accent"),
        up: v("up"),
        down: v("down"),
        warn: v("warn"),
        info: v("info"),
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
      },
      borderRadius: { card: "14px" },
    },
  },
  plugins: [],
} satisfies Config;
