import "server-only";

// APIの接続先はサーバー側の環境変数だけで持つ（ブラウザには公開しない）
const BASE = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";

export type ApiResult<T> = { ok: true; data: T } | { ok: false; status: number; error: string };

export async function api<T>(path: string): Promise<ApiResult<T>> {
  try {
    const res = await fetch(`${BASE}${path}`, { cache: "no-store", signal: AbortSignal.timeout(8000) });
    if (!res.ok) return { ok: false, status: res.status, error: res.status === 404 ? "見つかりません" : `APIエラー（${res.status}）` };
    return { ok: true, data: (await res.json()) as T };
  } catch {
    return { ok: false, status: 0, error: "分析APIに接続できません（バックエンドが起動していない可能性があります）" };
  }
}
