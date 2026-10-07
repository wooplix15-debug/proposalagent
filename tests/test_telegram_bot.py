import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from docx import Document
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from api.index import app
import multi_agent_graph
from telegram_bot import (
    AgentAPI, AgentAPIError, BotError, ConversationStore, MAX_UPLOAD,
    PollingService, TelegramBot, TelegramClient, text_chunks,
)


def message(text="", chat=101, **extra):
    return {"message": {"chat": {"id": chat, "type": "private"},
                        "from": {"id": chat, "first_name": "Reviewer"}, "text": text, **extra}}


def callback(action, chat=101):
    return {"callback_query": {"id": "callback-id", "data": action,
                               "from": {"id": chat, "first_name": "Reviewer"},
                               "message": {"chat": {"id": chat, "type": "private"}}}}


class FakeTelegram:
    def __init__(self):
        self.messages, self.documents, self.calls = [], [], []

    def message(self, chat, text, keyboard=None):
        self.messages.append((chat, text, keyboard))

    def document(self, chat, content, filename, caption=""):
        self.documents.append((chat, content, filename, caption))

    def call(self, method, data=None, **kwargs):
        self.calls.append((method, data))
        return {"username": "test_bot"} if method == "getMe" else True

    def download(self, document):
        return document["file_name"], b"CRM requirement file"


class TelegramBotTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.path = Path(self.work.name) / "telegram.sqlite"
        self.store = ConversationStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.telegram, self.api = FakeTelegram(), Mock()
        self.bot = TelegramBot(self.telegram, self.api, self.store)

    def clarification(self):
        return {"status": "interrupted", "stage": "clarification", "payload": {
            "summary": "CRM scope", "questions": [
                {"id": "q1", "question": "Which users?", "options": []},
                {"id": "q2", "question": "Training mode?", "options": ["Online", "On-site"]},
            ]}}

    def test_brd_upload_and_answers_survive_restart_and_stale_buttons_are_rejected(self):
        self.api.start.return_value = self.clarification()
        self.api.state.return_value = self.clarification()
        self.api.resume.return_value = {"status": "interrupted", "stage": "draft_review", "payload": {
            "document": {"summary": "CRM BRD", "specification_tables": [{"columns": ["Field", "Type"], "rows": [["Lead", "Text"]]}]}}}
        self.bot.handle_update(message("/brd"))
        self.bot.handle_update(message(document={"file_name": "Requirement.pdf", "file_id": "file-1"}))
        thread = self.store.load(101)["thread_id"]
        self.api.start.assert_called_once_with(thread, "brd", text=None, upload=("Requirement.pdf", b"CRM requirement file"))
        self.bot.handle_update(message("10 sales users"))
        self.store.close()
        self.store = ConversationStore(self.path)
        self.bot = TelegramBot(self.telegram, self.api, self.store)
        self.bot.handle_update(message("/status"))
        self.assertIn("Training mode?", self.telegram.messages[-1][1])
        self.bot.handle_update(callback(f"skip|{thread}|0"))
        self.api.resume.assert_not_called()
        self.assertIn("already answered", self.telegram.messages[-1][1])
        self.bot.handle_update(callback(f"skip|{thread}|1"))
        self.api.resume.assert_called_once_with(thread, {"answers": {
            "q1": "10 sales users", "q2": "Leave open for discovery"}})
        self.assertIn(b"Lead | Text", self.telegram.documents[-1][1])

    def test_failed_start_keeps_the_thread_for_retry_instead_of_redrafting(self):
        self.api.start.side_effect = AgentAPIError("Rate limit", 503)
        self.bot.handle_update(message("/proposal Configure lead management"))
        thread = self.store.load(101)["thread_id"]
        self.assertTrue(thread)
        self.api.state.return_value = {"status": "running", "retryable": True}
        self.api.retry.return_value = self.clarification()
        self.bot.handle_update(message("/retry"))
        self.api.retry.assert_called_once_with(thread)
        self.assertEqual(self.api.start.call_count, 1)

    def test_queued_work_reserves_the_chat_and_duplicate_replies_do_not_advance_it(self):
        reserved = self.bot.reserve_update(message("/proposal"))
        self.bot.handle_update(message("A second request while work is queued"))
        self.assertIn("still working", self.telegram.messages[-1][1])
        self.api.start.assert_not_called()
        self.bot.process_reserved(reserved)
        self.assertNotIn(101, self.bot.active)

    def test_other_chats_cannot_use_saved_case_buttons_and_groups_are_ignored(self):
        self.store.save(101, {"mode": "proposal", "thread_id": "owned-thread", "answers": {}})
        self.bot.handle_update(callback("docx|owned-thread", chat=202))
        self.api.export.assert_not_called()
        group = message("/proposal private text")
        group["message"]["chat"]["type"] = "group"
        self.bot.handle_update(group)
        self.api.start.assert_not_called()
        allowed_bot = TelegramBot(self.telegram, self.api, self.store, allowed_chats={202})
        allowed_bot.handle_update(message("/status"))
        self.api.state.assert_not_called()

    def test_skip_remaining_questions_is_explicit_and_not_an_implicit_approval(self):
        self.store.save(101, {"mode": "proposal", "thread_id": "case-1", "answers": {"q1": "Sales users"}})
        self.api.state.return_value = self.clarification()
        self.api.resume.return_value = {"status": "interrupted", "stage": "draft_review", "payload": {"document": {"summary": "CRM"}}}
        self.bot.handle_update(message("/generate"))
        self.api.resume.assert_called_once_with("case-1", {"answers": {"q1": "Sales users", "q2": "Leave open for discovery"}})
        self.api.export.assert_not_called()

    def test_real_persistent_api_flow_approval_and_saved_word_export(self):
        graph = multi_agent_graph.build_graph(InMemorySaver())
        client = TestClient(app)
        analysis = {"summary": "A CRM requirement.", "requirement_sections": [
            {"product": "Zoho CRM", "requirements": ["Lead management"]}],
            "questions": [], "commercial_categories": [], "duration_estimates": [],
            "context_record_ids": [], "context_sources": [], "comparisons": []}
        proposal = {
            "client": {"company_name": "Test Client", "project_name": "CRM", "contact": ""},
            "project_introduction": "Implement Zoho CRM.",
            "scope": [{"product": "Zoho CRM", "areas": [{"area": "Included work", "tasks": ["Lead management"]}]}],
            "prerequisites": [], "deliverables": [], "open_points": [], "status": "DRAFT",
        }

        def transport(method, url, **kwargs):
            kwargs.pop("timeout", None)
            output = client.request(method, url, **kwargs)
            response = requests.Response()
            response.status_code, response._content = output.status_code, output.content
            response.headers.update(output.headers)
            return response

        self.bot.api = AgentAPI("http://testserver")
        with patch("telegram_bot.requests.request", side_effect=transport), \
             patch("api.index.graph_runtime.get_graph", return_value=graph), \
             patch("api.index.agent.GROQ_API_KEY", "test"), \
             patch("multi_agent_graph.workflow.extract_inventory", return_value={}), \
             patch("multi_agent_graph.workflow.retrieve_evidence", return_value={}), \
             patch("multi_agent_graph.workflow.review_evidence", return_value={}), \
             patch("multi_agent_graph.workflow.assemble_analysis", return_value=analysis), \
             patch("multi_agent_graph.supervisor_specialist", return_value={"tasks": [
                 {"specialist": name} for name in ("solution", "delivery", "commercial", "risk")], "quality_focus": []}), \
             patch("multi_agent_graph.solution_specialist", return_value={"solutions": []}), \
             patch("multi_agent_graph.delivery_specialist", return_value={"timeline": {}}), \
             patch("multi_agent_graph.commercial_specialist", return_value={"commercials": {"items": []}}), \
             patch("multi_agent_graph.risk_specialist", return_value={"risks": []}), \
             patch("multi_agent_graph.workflow.prepare_draft", return_value=("Lead management", "reference")), \
             patch("multi_agent_graph.brand.draft_proposal", return_value=proposal) as writer, \
             patch("multi_agent_graph.finalize_client_content", side_effect=lambda p, a, q, r: p), \
             patch("multi_agent_graph.critic_specialist", return_value={"passed": True, "issues": []}):
            self.bot.handle_update(message("/proposal Lead management"))
            thread = self.store.load(101)["thread_id"]
            self.bot.handle_update(callback(f"generate|{thread}"))
            self.assertEqual(graph.get_state({"configurable": {"thread_id": thread}}).next, ("review",))
            self.bot.handle_update(message("/word"))
            self.assertEqual(len(self.telegram.documents), 1, "Only the draft preview is sent before approval")
            self.assertIn("Review the current draft", self.telegram.messages[-1][1])
            self.bot.handle_update(callback(f"approve|{thread}"))
            self.assertEqual(graph.get_state({"configurable": {"thread_id": thread}}).values["status"], "reviewed")
            self.bot.handle_update(callback(f"docx|{thread}"))
            chat, content, filename, caption = self.telegram.documents[-1]
            self.assertTrue(filename.endswith(".docx"))
            document = Document(io.BytesIO(content))
            self.assertIn("Implement Zoho CRM.", "\n".join(p.text for p in document.paragraphs))
            self.assertEqual(writer.call_count, 1, "Telegram export must render the saved draft")

    def test_polling_saves_its_update_offset_and_shuts_down_cleanly(self):
        service = PollingService(self.bot)
        calls = 0

        def call(method, data=None, **kwargs):
            nonlocal calls
            if method == "getMe":
                return {"username": "test_bot"}
            if method == "getUpdates":
                calls += 1
                if calls == 1:
                    return [{"update_id": 42, **message("/start")}]
                service.stop_event.set()
                return []
            return True

        self.telegram.call = call
        service.start()
        service.thread.join(3)
        service.stop()
        self.store = ConversationStore(self.path)
        self.assertEqual(self.store.offset(), 43)
        self.assertEqual(service.status, "stopped")
        self.assertTrue(any("Wooplix Proposal" in text for _, text, _ in self.telegram.messages))

    def test_telegram_token_is_redacted_from_network_errors_and_health(self):
        telegram = TelegramClient("secret-token")
        with patch("telegram_bot.requests.post", side_effect=requests.Timeout("https://api.telegram.org/botsecret-token/sendMessage")):
            with self.assertRaises(BotError) as failure:
                telegram.call("sendMessage")
            self.assertNotIn("secret-token", str(failure.exception))
        service = Mock(status="polling", username="example_bot")
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret-token"}), \
             patch("api.index.telegram_bot.start_configured_bot", return_value=service):
            with TestClient(app) as client:
                response = client.get("/api/health")
                self.assertNotIn("secret-token", response.text)
                self.assertEqual(response.json()["telegram"]["status"], "polling")
            service.stop.assert_called_once()

    def test_text_and_files_obey_telegram_and_local_upload_limits(self):
        chunks = list(text_chunks("😃" * 5000))
        self.assertEqual("".join(chunks), "😃" * 5000)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 3500 for chunk in chunks))
        telegram = TelegramClient("unused")
        with patch("telegram_bot.requests.post") as network:
            with self.assertRaises(BotError):
                telegram.download({"file_name": "Requirement.pdf", "file_size": MAX_UPLOAD + 1})
            with self.assertRaises(BotError):
                telegram.download({"file_name": "program.exe"})
            network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
