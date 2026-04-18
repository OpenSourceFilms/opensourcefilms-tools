"""
Import all actor modules here so Dramatiq discovers them when starting a worker.
"""
from workers.dramatiq_config import broker  # noqa: F401 — sets global broker

from ingest import probe       # noqa: F401
from ingest import segment     # noqa: F401
from ingest import keyframes   # noqa: F401
from ingest import asr         # noqa: F401
from ingest import ocr         # noqa: F401
