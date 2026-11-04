from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__
from ..config import get_settings
from ..db import get_session
from ..models import IngestionRun
from ..providers.registry import provider_statuses
from ..schemas import DatasetOut, ProviderOut, SystemStatus

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/status", response_model=SystemStatus)
def status(session: Session = Depends(get_session)) -> SystemStatus:
    provs = [ProviderOut(key=p.key, name=p.name, datasets=list(p.datasets), configured=p.configured, status=p.status,
                         message=p.message, missing_settings=p.missing_settings, url=p.url)
             for p in provider_statuses(get_settings())]
    latest = select(IngestionRun.dataset, func.max(IngestionRun.id)).where(IngestionRun.status == "success").group_by(IngestionRun.dataset)
    ids = [i for _, i in session.execute(latest)]
    runs = session.scalars(select(IngestionRun).where(IngestionRun.id.in_(ids))).all() if ids else []
    ds = [DatasetOut(dataset=r.dataset, source=r.source, status=r.status, rows=r.rows, source_as_of=r.source_as_of,
                     finished_at=r.finished_at) for r in sorted(runs, key=lambda r: r.dataset)]
    return SystemStatus(app_version=__version__, phase="フェーズ2（Webの基本構築）", providers=provs, datasets=ds,
                        generated_at=datetime.now(timezone.utc))
