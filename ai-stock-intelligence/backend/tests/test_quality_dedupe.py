"""品質の記録の重複防止のテスト（同じ開示日を取り直したとき「銘柄一覧にない」が110件重複した問題の回帰テスト）。"""
from datetime import date, datetime, timezone

from app import config
from app.db import SessionLocal
from app.models import DataQualityCheck
from app.providers import jquants as J
from app.services import ingest_jquants_fins as ING
from app.services.quality import add_once, forget_pending, remove_duplicates
from tests.test_jp_financials import rec


def test_refetching_same_date_does_not_duplicate_quality_records(client, monkeypatch):
    monkeypatch.setenv("NETWORK_SECRETS", "jquants")
    config.get_settings.cache_clear()

    class Fake:
        def __init__(self, *a, **k):
            pass

        def fins_summary(self, code=None, disc_date=None):
            return [rec("U1", "12340", "2026-05-14", "FYFinancialStatements_Consolidated_JP", "FY", "2025-04-01", "2026-03-31",
                        "2025-04-01", "2026-03-31", Sales=1),                                    # 銘柄一覧にない
                    rec("U1", "12340", "2026-05-14", "FYFinancialStatements_Consolidated_JP", "FY", "2025-04-01", "2026-03-31",
                        "2025-04-01", "2026-03-31", Sales=1),                                    # 同じ応答の中の重複
                    rec("X9", "12350", "2026-05-14", "SomethingNew", "FY", "2025-04-01", "2026-03-31", "2025-04-01", "2026-03-31")]

        def close(self):
            pass

    monkeypatch.setattr(J, "JQuantsClient", Fake)
    for _ in range(3):  # 同じ開示日を3回取得
        with SessionLocal() as s:
            assert ING.run(s, dates=[date(2026, 5, 14)]).status == "success"
    with SessionLocal() as s:
        q = lambda t: s.query(DataQualityCheck).filter_by(dataset="jquants_fins", check_type=t).count()
        assert q("unmatched") == 2 and q("unknown_doc_type") == 1  # U1・X9 が1件ずつ（X9 も一覧にない）
    config.get_settings.cache_clear()


def test_add_once_identity_and_rollback(client):
    with SessionLocal() as s:
        assert add_once(s, "d", "A", "t", {"doc_id": "1", "concept": "x"}, "info", {"doc_id": "1", "concept": "x"})
        assert not add_once(s, "d", "A", "t", {"doc_id": "1", "concept": "x"}, "info", {"doc_id": "1", "concept": "x"})
        assert add_once(s, "d", "A", "t", {"doc_id": "1", "concept": "y"}, "info", {"doc_id": "1", "concept": "y"})  # 項目が違う
        s.rollback()
        forget_pending(s)  # 保存されなかった記録を「記録済み」と覚えない
        assert add_once(s, "d", "A", "t", {"doc_id": "1", "concept": "x"}, "info", {"doc_id": "1", "concept": "x"})
        s.commit()
        assert s.query(DataQualityCheck).filter_by(dataset="d").count() == 1


def test_remove_duplicates_keeps_oldest_and_only_touches_quality(client):
    with SessionLocal() as s:
        for _ in range(3):
            s.add(DataQualityCheck(dataset="jquants_fins", subject="90670", check_type="unmatched", severity="info",
                                   detail={"disc_no": "D1", "reason": "r"}))
        s.add(DataQualityCheck(dataset="jquants_fins", subject="90670", check_type="unmatched", severity="info",
                               detail={"disc_no": "D2", "reason": "r"}))
        s.commit()
        first = min(q.id for q in s.query(DataQualityCheck).filter_by(subject="90670"))
        assert remove_duplicates(s) == 2
        left = s.query(DataQualityCheck).filter_by(subject="90670").all()
        assert len(left) == 2 and first in {q.id for q in left}
