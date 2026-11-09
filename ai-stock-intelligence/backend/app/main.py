"""FastAPI アプリ。分析結果をデータベースから返すだけの読み取り中心のAPI（設計書 2章）。"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .db import init_db
from .routers import macro, market, securities, system


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DATABASE_URL の検査（未設定・相対パス・存在しないDBなら、ここで止まる。既定のDBを黙って作らない）
    init_db()
    from .db import db_identity
    logging.getLogger("app").info("データベース：fingerprint=%s", db_identity()["fingerprint"])
    from .config import get_settings
    st = get_settings()
    if st.jquants_plan_status == "invalid":  # 設定不備でも起動する（J-Quants の取得だけを止める）。ログで知らせる
        from .services.jquants_plan import invalid_message
        logging.getLogger("app").error("設定不備：%s", invalid_message(st))
    yield


app = FastAPI(title="AI STOCK INTELLIGENCE API", version=__version__, lifespan=lifespan)
app.include_router(system.router)
app.include_router(market.router)
app.include_router(securities.router)
app.include_router(macro.router)


@app.get("/healthz")
def healthz() -> dict:
    # db.fingerprint：API が使っている DB の識別用の短いハッシュ（パス・パスワードは含めない）。
    # ワーカーの check-db（または DB_CHECK_API_URL）が自分の値と比べ、違う DB を使っていれば止まる
    from .db import db_identity
    return {"ok": True, "db": {"fingerprint": db_identity()["fingerprint"]}}
