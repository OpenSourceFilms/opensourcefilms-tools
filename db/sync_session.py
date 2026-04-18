"""
Synchronous SQLAlchemy session for use in Dramatiq workers.
Workers are thread-based — async engines cause 'another operation in progress' errors.
"""
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from config.settings import settings

# Convert asyncpg URL to psycopg2 URL
_sync_url = settings.postgres_url.replace(
    "postgresql+asyncpg://", "postgresql+psycopg2://"
)

_engine = create_engine(
    _sync_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
)

SyncSessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


@contextmanager
def get_sync_db() -> Session:
    db = SyncSessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
