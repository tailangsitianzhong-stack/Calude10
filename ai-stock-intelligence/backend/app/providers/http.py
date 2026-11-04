"""外部APIへのHTTPアクセスの共通処理：レート制限、再試行、タイムアウト。

失敗しても例外を上位に伝え、取得処理ごとに失敗を記録する（他のデータ取得は止めない）。
"""
import re
import time

import httpx

# URL に含まれる秘密情報（APIキー）を、ログ・エラーメッセージ・画面に出さないために伏せる
_SECRET_PARAMS = re.compile(r"((?:api_?key|apikey|token|access_token)=)[^&\s\"']+", re.IGNORECASE)


def redact(text: str) -> str:
    return _SECRET_PARAMS.sub(r"\1***", text or "")


class HttpError(RuntimeError):
    """HTTPエラー。メッセージにはAPIキーを含めない。"""

    def __init__(self, status_code: int, url: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {redact(url)}")


def check(r: httpx.Response) -> httpx.Response:
    """raise_for_status の代わり。httpx の例外はURL（APIキーを含む場合がある）をそのまま含むため使わない。"""
    if r.status_code >= 400:
        raise HttpError(r.status_code, str(r.request.url))
    return r


class RateLimitedClient:
    def __init__(self, min_interval: float, headers: dict | None = None, timeout: float = 60.0, retries: int = 4):
        self.min_interval = min_interval
        self.retries = retries
        self._last = 0.0
        self.client = httpx.Client(timeout=timeout, headers=headers or {}, follow_redirects=True)

    def get(self, url: str, **kw) -> httpx.Response:
        delay = 2.0
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.client.get(url, **kw)
            except httpx.TransportError as e:
                if attempt == self.retries:
                    raise RuntimeError(f"通信エラー：{type(e).__name__} {redact(url)}") from None
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                time.sleep(float(r.headers.get("Retry-After", delay)))
                delay *= 2
                continue
            return r
        raise RuntimeError("unreachable")

    def close(self) -> None:
        self.client.close()
