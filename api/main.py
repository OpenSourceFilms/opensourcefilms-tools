import logging
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.routes import projects, assets, health, queue_status

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Media Evidence Retrieval API",
    version="0.1.0",
    description="Phase 0 — ingest skeleton",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(projects.router, prefix="/projects", tags=["projects"])
app.include_router(assets.router, prefix="/projects", tags=["assets"])
app.include_router(queue_status.router, prefix="/queue", tags=["queue"])


@app.on_event("startup")
async def _startup():
    from store.client import ensure_buckets
    try:
        ensure_buckets()
        logger.info("MinIO buckets verified")
    except Exception as exc:
        logger.warning("MinIO bucket check failed (will retry on requests): %s", exc)
