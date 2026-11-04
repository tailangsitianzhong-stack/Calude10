// 実データの系列だけを描く折れ線（SVG）。点が2つ未満なら何も描かない。
export function LineChart({ points, height = 140, label, unit = "", showAxis = true }: {
  points: [string, number][]; height?: number; label: string; unit?: string; showAxis?: boolean;
}) {
  if (points.length < 2) return null;
  const W = 600, H = height, pad = showAxis ? 26 : 4;
  const vals = points.map((p) => p[1]);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const x = (i: number) => pad + (i / (points.length - 1)) * (W - pad * 2);
  const y = (v: number) => 6 + (1 - (v - min) / span) * (H - 6 - (showAxis ? 22 : 6));
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const up = vals[vals.length - 1] >= vals[0];
  const color = up ? "rgb(var(--up))" : "rgb(var(--down))";
  const id = `g-${label.replace(/[^a-zA-Z0-9]/g, "")}-${points.length}`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label={`${label}の推移（${points[0][0]}〜${points[points.length - 1][0]}）`}>
      <defs>
        <linearGradient id={id} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor={color} stopOpacity="0.25" />
          <stop offset="1" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${d}L${x(points.length - 1)},${H - (showAxis ? 22 : 6)}L${x(0)},${H - (showAxis ? 22 : 6)}Z`} fill={`url(#${id})`} />
      <path d={d} fill="none" stroke={color} strokeWidth="1.8" vectorEffect="non-scaling-stroke" />
      {showAxis && (
        <g fill="rgb(var(--faint))" fontSize="11" className="num">
          <text x={pad} y={H - 6}>{points[0][0]}</text>
          <text x={W - pad} y={H - 6} textAnchor="end">{points[points.length - 1][0]}</text>
          <text x={W - pad} y={14} textAnchor="end">高 {max.toLocaleString()}{unit}</text>
          <text x={W - pad} y={H - 26} textAnchor="end">安 {min.toLocaleString()}{unit}</text>
        </g>
      )}
    </svg>
  );
}
