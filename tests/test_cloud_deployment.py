import os
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from api.index import app
import graph_runtime
from make_vercel_env import build_env
import telegram_bot
import telegram_cloud


class DedupStore:
    def __init__(self):
        self.claimed, self.completed = set(), []

    def claim_update(self, update_id):
        if update_id in self.claimed:
            return False
        self.claimed.add(update_id)
        return True

    def complete_update(self, update_id, status):
        self.completed.append((update_id, status))


class CloudDeploymentTests(unittest.TestCase):
    def test_webhook_authentication_validation_and_deduplication(self):
        bot = Mock(store=DedupStore())
        update = {"update_id": 17, "message": {"text": "/start"}}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_WEBHOOK_SECRET": "private-secret"}), \
             patch("api.index.telegram_cloud.get_bot", return_value=bot) as factory:
            client = TestClient(app)
            self.assertEqual(client.post("/api/telegram/webhook", json=update).status_code, 401)
            factory.assert_not_called()
            headers = {"X-Telegram-Bot-Api-Secret-Token": "private-secret"}
            self.assertEqual(client.post("/api/telegram/webhook", json={"update_id": True}, headers=headers).status_code, 400)
            for _ in range(2):
                result = client.post("/api/telegram/webhook", json=update, headers=headers)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json(), {"ok": True})
                self.assertNotIn("private-secret", result.text)
            bot.handle_update.assert_called_once_with(update)
            self.assertEqual(bot.store.completed, [(17, "completed")])

    def test_webhook_storage_failure_is_retryable_instead_of_acknowledged(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_WEBHOOK_SECRET": "secret"}), \
             patch("api.index.telegram_cloud.get_bot", side_effect=RuntimeError("Unavailable")):
            result = TestClient(app).post("/api/telegram/webhook", json={"update_id": 1},
                                          headers={"X-Telegram-Bot-Api-Secret-Token": "secret"})
            self.assertEqual(result.status_code, 503)

    def test_background_delivery_error_is_recorded(self):
        bot = Mock(store=DedupStore())
        bot.handle_update.side_effect = RuntimeError("Delivery failed")
        telegram_cloud.process_update(bot, {"update_id": 22})
        self.assertEqual(bot.store.completed, [(22, "failed")])

    def test_vercel_never_starts_a_polling_thread_or_uses_local_sqlite(self):
        with patch.dict(os.environ, {"VERCEL": "1", "TELEGRAM_POLLING_ENABLED": "1", "LANGGRAPH_STORAGE": "postgres"}), \
             patch("telegram_bot.PollingService") as polling:
            self.assertIsNone(telegram_bot.start_configured_bot())
            polling.assert_not_called()
            self.assertEqual(graph_runtime.storage_backend(), "postgres")
        with patch.dict(os.environ, {"VERCEL": "1", "LANGGRAPH_STORAGE": "sqlite"}):
            with self.assertRaises(RuntimeError):
                graph_runtime.storage_backend()

    def test_cloud_chat_lease_blocks_another_instance_and_is_released(self):
        store = Mock()
        store.acquire.return_value = False
        telegram = Mock()
        bot = telegram_bot.TelegramBot(telegram, Mock(), store)
        update = {"message": {"chat": {"id": 101, "type": "private"}, "from": {"id": 101}, "text": "/help"}}
        self.assertIsNone(bot.reserve_update(update))
        self.assertIn("still working", telegram.message.call_args.args[1])
        store.acquire.return_value = True
        context = bot.reserve_update(update)
        bot.process_reserved(context)
        store.release.assert_called_once_with(101, context[-1])

    def test_cloud_base_url_uses_deployment_host_without_localhost(self):
        with patch.dict(os.environ, {"TELEGRAM_AGENT_BASE_URL": "", "APP_BASE_URL": "", "VERCEL_PROJECT_PRODUCTION_URL": "proposalagent.vercel.app"}):
            self.assertEqual(telegram_cloud.app_base_url(), "https://proposalagent.vercel.app")

    def test_vercel_env_uses_neon_and_webhooks_and_reuses_generated_secrets(self):
        source = {"GROQ_API_KEY": "groq-key", "TELEGRAM_BOT_TOKEN": "bot-token",
                  "DATABASE_URL": "postgresql://pooler", "DATABASE_URL_UNPOOLED": "postgresql://direct",
                  "TELEGRAM_AGENT_BASE_URL": "http://127.0.0.1:8001", "LANGGRAPH_SQLITE_PATH": ".state/file.sqlite"}
        first = build_env(source)
        second = build_env(source, first)
        self.assertEqual(first, second)
        self.assertEqual(first["LANGGRAPH_STORAGE"], "postgres")
        self.assertEqual(first["TELEGRAM_MODE"], "webhook")
        self.assertEqual(first["TELEGRAM_POLLING_ENABLED"], "0")
        self.assertNotIn("TELEGRAM_AGENT_BASE_URL", first)
        self.assertNotIn("LANGGRAPH_SQLITE_PATH", first)
        self.assertNotEqual(first["PDF_RENDER_TOKEN"], first["TELEGRAM_WEBHOOK_SECRET"])


if __name__ == "__main__":
    unittest.main()
