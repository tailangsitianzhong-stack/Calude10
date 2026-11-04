// 総合評価のゲージ。score が null のときは空のリングと「未評価」を表示し、数値を作らない。
export function ScoreGauge({ score, label }: { score: number | null; label?: string }) {
  const r = 42, c = 2 * Math.PI * r;
  const pct = score === null ? 0 : Math.max(0, Math.min(100, score)) / 100;
  return (
    <figure className="flex items-center gap-4" aria-label={score === null ? "総合評価：未評価" : `総合評価 ${score}点`}>
      <svg width="104" height="104" viewBox="0 0 104 104" role="img" aria-hidden>
        <circle cx="52" cy="52" r={r} fill="none" stroke="rgb(var(--line))" strokeWidth="9" strokeDasharray="4 4" />
        {score !== null && (
          <circle cx="52" cy="52" r={r} fill="none" stroke="rgb(var(--accent))" strokeWidth="9" strokeLinecap="round"
            strokeDasharray={`${c * pct} ${c}`} transform="rotate(-90 52 52)" />
        )}
        <text x="52" y="50" textAnchor="middle" className="num" fill="rgb(var(--ink))" fontSize="22" fontWeight="700">{score === null ? "—" : score}</text>
        <text x="52" y="68" textAnchor="middle" fill="rgb(var(--faint))" fontSize="10">/ 100</text>
      </svg>
      <figcaption className="text-xs leading-relaxed text-muted">
        <div className="text-sm font-semibold text-ink">{score === null ? "未評価" : label}</div>
        投資魅力度の評価です。<br />株価が上がる確率ではありません。
      </figcaption>
    </figure>
  );
}
