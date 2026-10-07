"""Shared Neon/PostgreSQL pool and idempotent application migrations."""
import os
from pathlib import Path
from threading import Lock

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

HERE = Path(__file__).resolve().parent
_POOL = None
_LOCK = Lock()
_MIGRATION_LOCK = 574946706578072843


def setup_database():
    """Use the direct endpoint for migrations; serialize concurrent cold starts."""
    url = os.environ.get("DATABASE_URL_UNPOOLED") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set DATABASE_URL before using PostgreSQL persistence.")
    with Connection.connect(url, autocommit=True, row_factory=dict_row,
                            prepare_threshold=None, connect_timeout=15) as connection:
        connection.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
        try:
            PostgresSaver(connection).setup()
            sql = (HERE / "migrations" / "001_telegram.sql").read_text(encoding="utf-8")
            for statement in sql.split(";"):
                if statement.strip():
                    connection.execute(statement)
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))


def get_pool():
    global _POOL
    with _LOCK:
        if _POOL is None:
            url = os.environ.get("DATABASE_URL")
            if not url:
                raise RuntimeError("DATABASE_URL is required for durable Vercel case storage.")
            setup_database()
            pool = ConnectionPool(
                url, min_size=0, max_size=4, timeout=30, max_idle=60,
                kwargs={"autocommit": True, "row_factory": dict_row,
                        "prepare_threshold": None, "connect_timeout": 15},
                check=ConnectionPool.check_connection, open=True,
            )
            _POOL = pool
    return _POOL


def close_pool():
    global _POOL
    with _LOCK:
        if _POOL:
            _POOL.close()
            _POOL = None
