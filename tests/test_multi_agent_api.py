import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from api.index import app
import multi_agent_graph as agent_graph
import graph_runtime


class MultiAgentAPITests(unittest.TestCase):
    def test_sqlite_runtime_keeps_a_case_after_connection_restart(self):
        config = {"configurable": {"thread_id": "durable-case"}}
        with tempfile.TemporaryDirectory() as work, \
             patch.object(graph_runtime, "DB_PATH", Path(work) / "state.sqlite"), \
             patch.object(graph_runtime, "_GRAPH", None), \
             patch.object(graph_runtime, "_CONNECTION", None):
            first = graph_runtime.get_graph()
            first.update_state(config, {"mode": "case", "document_type": "proposal", "status": "reviewed",
                                        "trace": ["quality_critic: passed"]}, as_node="finish")
            graph_runtime._CONNECTION.close()
            graph_runtime._GRAPH = None
            second = graph_runtime.get_graph()
            saved = second.get_state(config)
            self.assertEqual(saved.values["status"], "reviewed")
            self.assertIn("quality_critic: passed", saved.values["trace"])
            graph_runtime._CONNECTION.close()

    def test_thread_not_found_is_not_a_successful_resume(self):
        graph = agent_graph.build_graph(InMemorySaver())
        with patch("api.index.graph_runtime.get_graph", return_value=graph):
            client = TestClient(app)
            response = client.post("/api/agent/resume", json={"thread_id": "missing", "response": {}})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(client.get("/api/agent/state/missing").status_code, 404)

    def test_review_gate_exports_saved_document_without_redrafting(self):
        graph = agent_graph.build_graph(InMemorySaver())
        document = {
            "client": {"company_name": "Test Client", "project_name": "CRM", "contact": ""},
            "project_introduction": "Configure the requested CRM.",
            "scope": [{"product": "Zoho CRM", "areas": [{"area": "Leads", "tasks": ["Lead management"]}]}],
            "prerequisites": [], "deliverables": [], "status": "DRAFT",
        }
        config = {"configurable": {"thread_id": "review-test"}}
        graph.update_state(config, {"mode": "case", "document_type": "proposal",
                                   "proposal": document, "status": "draft_ready"}, as_node="finish")
        with patch("api.index.graph_runtime.get_graph", return_value=graph), \
             patch("api.index.multi_agent_graph.run_proposal") as generation:
            client = TestClient(app)
            self.assertEqual(client.get("/api/agent/export/review-test?format=docx").status_code, 409)
            graph.update_state(config, {"status": "reviewed", "reviewed": True}, as_node="finish")
            output = client.get("/api/agent/export/review-test?format=docx")
            self.assertEqual(output.status_code, 200)
            self.assertIn("Test_Client", output.headers["x-proposal-filename"])
            doc = Document(io.BytesIO(output.content))
            self.assertIn("Configure the requested CRM.", "\n".join(p.text for p in doc.paragraphs))
            generation.assert_not_called()

    def test_existing_thread_cannot_be_overwritten_by_start(self):
        graph = agent_graph.build_graph(InMemorySaver())
        graph.update_state({"configurable": {"thread_id": "existing"}},
                           {"status": "reviewed", "document_type": "proposal"}, as_node="finish")
        with patch("api.index.graph_runtime.get_graph", return_value=graph), \
             patch("api.index.agent.GROQ_API_KEY", "test"):
            response = TestClient(app).post("/api/agent/start", data={
                "thread_id": "existing", "text": "Zoho CRM lead management", "document_type": "proposal"
            })
            self.assertEqual(response.status_code, 409)
