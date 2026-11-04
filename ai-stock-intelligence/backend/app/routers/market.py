from fastapi import APIRouter

from ..schemas import IndexQuote

router = APIRouter(prefix="/api/v1/market", tags=["market"])

INDICES = (("nikkei225", "日経平均株価", "JP"), ("topix", "TOPIX", "JP"), ("sp500", "S&P 500", "US"), ("nasdaq", "NASDAQ総合", "US"))


@router.get("/overview", response_model=list[IndexQuote])
def overview() -> list[IndexQuote]:
    # フェーズ2では指数の取得処理がないため、値は返さない（架空の値を作らない）
    return [IndexQuote(key=k, name=n, market=m, value=None, change_pct=None, as_of=None, provider=None, is_delayed=None,
                       data_status="not_connected", note="指数データの取得はフェーズ3で実装予定")
            for k, n, m in INDICES]
