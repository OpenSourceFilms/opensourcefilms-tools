"""
RQ (Redis Queue) configuration.
Replaces Dramatiq. Workers run as: rq worker ingest ingest_gpu
"""
from redis import Redis
from rq import Queue

from config.settings import settings

redis_conn = Redis.from_url(settings.redis_url)

ingest_queue = Queue("ingest", connection=redis_conn, default_timeout=3600)
ingest_gpu_queue = Queue("ingest_gpu", connection=redis_conn, default_timeout=7200)
embed_queue = Queue("embed", connection=redis_conn, default_timeout=7200)
