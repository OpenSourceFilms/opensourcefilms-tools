"""
RQ (Redis Queue) configuration.
Replaces Dramatiq. Workers run as:
  rq worker ingest ingest_gpu          (pipeline-v2 venv)
  rq worker embed                      (qwen3_embed_clean venv)
  rq worker --with-scheduler objects   (groundingdino venv)
"""
from redis import Redis
from rq import Queue

from config.settings import settings

redis_conn = Redis.from_url(settings.redis_url)

ingest_queue = Queue("ingest", connection=redis_conn, default_timeout=3600)
ingest_gpu_queue = Queue("ingest_gpu", connection=redis_conn, default_timeout=7200)
embed_queue = Queue("embed", connection=redis_conn, default_timeout=7200)
objects_queue = Queue("objects", connection=redis_conn, default_timeout=3600)
