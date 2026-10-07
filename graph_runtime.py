"""Local SQLite-backed LangGraph runtime.

SQLite is intentionally used for the local/internal prototype so there is no
separate database service to install. Set LANGGRAPH_SQLITE_PATH to move the
file. Production deployments should replace this checkpointer with Postgres.
"""
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


def get_graph():
    """Return one process-local graph using the SQLite checkpointer."""
    global _GRAPH, _CONNECTION
    with _INIT_LOCK:
        if _GRAPH is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CONNECTION = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
            checkpointer = SqliteSaver(_CONNECTION)
            checkpointer.setup()
            _GRAPH = multi_agent_graph.build_graph(checkpointer=checkpointer)
    return _GRAPH
