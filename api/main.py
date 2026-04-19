import logging
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.routes import projects, assets, health, queue_status, search, characters

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Media Evidence Retrieval API",
    version="0.2.0",
    description="Phase 2 — ingest + multi-surface semantic search + entity enrichment",
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
app.include_router(search.router, prefix="/projects", tags=["search"])
app.include_router(characters.router, prefix="", tags=["characters"])


@app.on_event("startup")
async def _startup():
    from store.client import ensure_buckets
    try:
        ensure_buckets()
        logger.info("MinIO buckets verified")
    except Exception as exc:
        logger.warning("MinIO bucket check failed (will retry on requests): %s", exc)

# Import is appended below to avoid circular issues at module level
