"""Verify cloud migrations, checkpoints and Telegram state using a Neon branch."""
import argparse
import os
from pathlib import Path
from uuid import uuid4

from make_vercel_env import read_env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, default=Path(".env"))
    args = parser.parse_args()
    values = read_env(args.env)
    os.environ.update({key: values[key] for key in ("DATABASE_URL", "DATABASE_URL_UNPOOLED")})
    os.environ["LANGGRAPH_STORAGE"] = "postgres"
    # Import after selecting the test branch, so dotenv cannot override it.
    import graph_runtime
    import postgres_runtime
    from telegram_cloud import PostgresConversationStore

    thread = "cloud-smoke-" + str(uuid4())
    config = {"configurable": {"thread_id": thread}}
    namespace = "smoke-" + str(uuid4())
    try:
        graph = graph_runtime.get_graph()
        graph.update_state(config, {"mode": "case", "document_type": "proposal", "status": "reviewed",
                                    "trace": ["complete: reviewed"], "reviewed": True}, as_node="finish")
        store = PostgresConversationStore(postgres_runtime.get_pool(), namespace)
        session = {"mode": "brd", "thread_id": thread, "answers": {"q1": "10 users"}}
        store.save(101, session)
        assert store.acquire(101, "first-owner")
        assert not store.acquire(101, "second-owner")
        store.release(101, "wrong-owner")
        assert not store.acquire(101, "second-owner")
        store.release(101, "first-owner")
        assert store.acquire(101, "second-owner")
        store.release(101, "second-owner")
        assert store.claim_update(42)
        assert not store.claim_update(42)
        store.complete_update(42)
        postgres_runtime.close_pool()
        graph_runtime._GRAPH = None
        restored = graph_runtime.get_graph().get_state(config)
        assert restored.values["status"] == "reviewed" and restored.values["trace"] == ["complete: reviewed"]
        restarted = PostgresConversationStore(postgres_runtime.get_pool(), namespace)
        assert restarted.load(101) == session
        assert not restarted.claim_update(42)
        print("PostgreSQL migrations, checkpoint restart persistence, conversation persistence, chat leases and update deduplication passed.")
    finally:
        if graph_runtime._GRAPH is not None:
            graph_runtime._GRAPH.checkpointer.delete_thread(thread)
        if postgres_runtime._POOL is not None:
            with postgres_runtime.get_pool().connection() as connection:
                connection.execute("DELETE FROM wooplix_telegram_chats WHERE bot_id=%s", (namespace,))
                connection.execute("DELETE FROM wooplix_telegram_updates WHERE bot_id=%s", (namespace,))
            postgres_runtime.close_pool()


if __name__ == "__main__":
    main()
