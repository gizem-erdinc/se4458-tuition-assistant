"""PostgreSQL access (psycopg 3 + connection pool).

DATABASE_URL, e.g. postgresql://tuition:tuition@localhost:5432/tuition
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_URL = "postgresql://postgres:postgres@localhost:5432/tuition"

_pool: ConnectionPool | None = None


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(database_url(), min_size=1, max_size=int(os.environ.get("DB_POOL_SIZE", "10")),
                               kwargs={"row_factory": dict_row, "autocommit": True}, open=True)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def init_db() -> None:
    with psycopg.connect(database_url(), autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(4458)")      # several replicas may start at once
        try:
            conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        finally:
            conn.execute("SELECT pg_advisory_unlock(4458)")


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Autocommit connection from the pool; write paths open an explicit
    ``with conn.transaction():`` block (nested blocks become SAVEPOINTs)."""
    with get_pool().connection() as conn:
        yield conn
