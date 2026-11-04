"""外部APIへのHTTPアクセスの共通処理：レート制限、再試行、タイムアウト。

失敗しても例外を上位に伝え、取得処理ごとに失敗を記録する（他のデータ取得は止めない）。
"""
import time

import httpx


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
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
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
