"""
Worker module — exposes queue handles.
RQ discovers job functions by their dotted import path at job execution time,
so eager imports of ingest modules here are not required (and break venvs
that lack heavy deps like cv2, paddle, etc.).
"""
from workers.rq_config import ingest_queue, ingest_gpu_queue, embed_queue  # noqa
