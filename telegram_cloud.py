"""Durable webhook conversations, deduplication and cross-instance chat leases."""
import logging
import os
from threading import Lock

from psycopg.types.json import Jsonb

import postgres_runtime
from telegram_bot import AgentAPI, TelegramBot, TelegramClient

LOG = logging.getLogger(__name__)
_BOT = None
_LOCK = Lock()


class PostgresConversationStore:
    def __init__(self, pool, bot_id):
        self.pool, self.bot_id = pool, str(bot_id)

    def load(self, chat):
        with self.pool.connection() as connection:
            row = connection.execute(
                "SELECT state FROM wooplix_telegram_chats WHERE bot_id=%s AND chat_id=%s",
                (self.bot_id, chat),
            ).fetchone()
        return row["state"] if row else {"mode": "proposal", "thread_id": None, "answers": {}}

    def save(self, chat, state):
        with self.pool.connection() as connection:
            connection.execute(
                "INSERT INTO wooplix_telegram_chats (bot_id,chat_id,state) VALUES (%s,%s,%s) "
                "ON CONFLICT (bot_id,chat_id) DO UPDATE SET state=EXCLUDED.state, updated_at=NOW()",
                (self.bot_id, chat, Jsonb(state)),
            )

    def acquire(self, chat, owner):
        with self.pool.connection() as connection:
            row = connection.execute(
                "INSERT INTO wooplix_telegram_chats (bot_id,chat_id,lease_owner,lease_until) "
                "VALUES (%s,%s,%s,NOW()+INTERVAL '330 seconds') "
                "ON CONFLICT (bot_id,chat_id) DO UPDATE SET lease_owner=EXCLUDED.lease_owner, "
                "lease_until=EXCLUDED.lease_until WHERE wooplix_telegram_chats.lease_until IS NULL "
                "OR wooplix_telegram_chats.lease_until<NOW() RETURNING chat_id",
                (self.bot_id, chat, owner),
            ).fetchone()
        return row is not None

    def release(self, chat, owner):
        with self.pool.connection() as connection:
            connection.execute(
                "UPDATE wooplix_telegram_chats SET lease_owner=NULL,lease_until=NULL "
                "WHERE bot_id=%s AND chat_id=%s AND lease_owner=%s",
                (self.bot_id, chat, owner),
            )

    def claim_update(self, update_id):
        with self.pool.connection() as connection:
            row = connection.execute(
                "INSERT INTO wooplix_telegram_updates (bot_id,update_id) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING update_id", (self.bot_id, update_id),
            ).fetchone()
        return row is not None

    def complete_update(self, update_id, status="completed"):
        with self.pool.connection() as connection:
            connection.execute(
                "UPDATE wooplix_telegram_updates SET status=%s,completed_at=NOW() WHERE bot_id=%s AND update_id=%s",
                (status, self.bot_id, update_id),
            )


def app_base_url():
    base = (os.environ.get("TELEGRAM_AGENT_BASE_URL") or os.environ.get("APP_BASE_URL") or
            os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL"))
    if not base:
        raise RuntimeError("Set APP_BASE_URL or deploy on Vercel before enabling the webhook.")
    return base.rstrip("/") if base.startswith("http") else "https://" + base.rstrip("/")


def get_bot():
    global _BOT
    with _LOCK:
        if _BOT is None:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
            if not token:
                raise RuntimeError("Set TELEGRAM_BOT_TOKEN before enabling the webhook.")
            store = PostgresConversationStore(postgres_runtime.get_pool(), token.split(":", 1)[0])
            allowed = [int(chat.strip()) for chat in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if chat.strip()]
            _BOT = TelegramBot(TelegramClient(token), AgentAPI(app_base_url()), store, allowed)
    return _BOT


def process_update(bot, update):
    """Run as an ASGI background task after Telegram receives its acknowledgment."""
    status = "completed"
    try:
        bot.handle_update(update)
    except Exception as exc:
        status = "failed"
        LOG.warning("Telegram webhook delivery failed (%s)", type(exc).__name__)
    finally:
        bot.store.complete_update(update["update_id"], status)
