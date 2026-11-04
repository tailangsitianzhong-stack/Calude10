import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-md py-16 text-center">
      <p className="num text-5xl font-bold text-faint">404</p>
      <h1 className="mt-3 text-lg font-semibold">ページまたは銘柄が見つかりません</h1>
      <p className="mt-2 text-sm text-muted">銘柄コード・ティッカーを確認してください。</p>
      <Link href="/search" className="mt-6 inline-block rounded-xl bg-accent px-5 py-2.5 text-sm font-semibold text-bg">銘柄検索へ</Link>
    </div>
  );
}
