from fastapi import APIRouter
import redis as redis_lib

from config.settings import settings

router = APIRouter()


@router.get("/status")
def queue_status():
    try:
        r = redis_lib.from_url(settings.redis_url)
        queues = ["ingest", "ingest_gpu"]
        info = {}
        for q in queues:
            depth = r.llen(f"dramatiq:{q}.msgs")
            info[q] = {"depth": depth}
        return {"status": "ok", "queues": info}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}
