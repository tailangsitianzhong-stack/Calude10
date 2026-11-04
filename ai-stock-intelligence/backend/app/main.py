"""FastAPI アプリ。分析結果をデータベースから返すだけの読み取り中心のAPI（設計書 2章）。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__
from .db import init_db
from .routers import macro, market, securities, system


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="AI STOCK INTELLIGENCE API", version=__version__, lifespan=lifespan)
app.include_router(system.router)
app.include_router(market.router)
app.include_router(securities.router)
app.include_router(macro.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
