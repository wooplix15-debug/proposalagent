"""SQLite locally; durable PostgreSQL checkpoints for the deployed app."""
from __future__ import annotations

import os
import sqlite3
from threading import Lock
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

import multi_agent_graph


HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("LANGGRAPH_SQLITE_PATH", HERE / ".state" / "wooplix.sqlite"))
if not DB_PATH.is_absolute():
    DB_PATH = HERE / DB_PATH
_GRAPH = None
_CONNECTION = None
_INIT_LOCK = Lock()


def storage_backend():
    backend = os.environ.get("LANGGRAPH_STORAGE") or ("postgres" if os.environ.get("VERCEL") else "sqlite")
    if backend not in {"postgres", "sqlite"}:
        raise RuntimeError("LANGGRAPH_STORAGE must be postgres or sqlite.")
    if os.environ.get("VERCEL") and backend != "postgres":
        raise RuntimeError("Vercel requires PostgreSQL persistence; set LANGGRAPH_STORAGE=postgres.")
    return backend


def get_graph():
    """Reuse a compiled graph; cloud data is retained in PostgreSQL, not memory."""
    global _GRAPH, _CONNECTION
    with _INIT_LOCK:
        if _GRAPH is None:
            if storage_backend() == "postgres":
                from langgraph.checkpoint.postgres import PostgresSaver
                from postgres_runtime import get_pool
                checkpointer = PostgresSaver(get_pool())
            else:
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                _CONNECTION = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
                checkpointer = SqliteSaver(_CONNECTION)
                checkpointer.setup()
            _GRAPH = multi_agent_graph.build_graph(checkpointer=checkpointer)
    return _GRAPH
