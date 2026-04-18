from fastapi import APIRouter
from sqlalchemy import text

from db.base import AsyncSessionLocal

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    db_ok = False
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
    }
