import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from api.index import app
import multi_agent_graph
import workflow_view


class WorkflowViewTests(unittest.TestCase):
    def test_exact_topology_and_folded_routes_match_the_compiled_graph(self):
        actual = multi_agent_graph._STATELESS_GRAPH.get_graph()
        with patch("api.index.graph_runtime.get_graph") as persistence, \
             patch.object(multi_agent_graph._STATELESS_GRAPH, "invoke") as generation:
            response = TestClient(app).get("/api/agent/workflow")
            self.assertEqual(response.status_code, 200)
            result = response.json()
            persistence.assert_not_called()
            generation.assert_not_called()
        self.assertEqual(set(actual.nodes), set(workflow_view.NODES))
        self.assertEqual({item["id"] for item in result["nodes"]}, set(actual.nodes))
        self.assertEqual({(item["source"], item["target"], item["conditional"]) for item in result["edges"]},
                         {(item.source, item.target, item.conditional) for item in actual.edges})
        groups = {member: item["id"] for item in result["overview_nodes"] for member in item["members"]}
        expected = {(groups[item.source], groups[item.target]) for item in actual.edges
                    if item.target != "__end__" and groups[item.source] != groups[item.target]}
        self.assertEqual({(item["source"], item["target"]) for item in result["overview_edges"]}, expected)
        self.assertEqual(result["summary"]["main_roles_per_run"], 8)
        self.assertEqual(result["summary"]["unique_role_implementations"], 10)

    def test_real_interrupt_is_waiting_and_successful_trace_steps_are_highlighted(self):
        graph = multi_agent_graph.build_graph(InMemorySaver())
        config = {"configurable": {"thread_id": "clarification-test"}}
        graph.update_state(config, {
            "document_type": "proposal", "status": "running",
            "analysis": {"summary": "Lead management", "requirement_sections": [],
                         "questions": [], "duration_estimates": []},
            "trace": ["supervisor: case accepted", "requirement_agent: source checklist extracted"],
        }, as_node="assemble")
        graph.invoke(None, config)
        before = graph.get_state(config)
        with patch("api.index.graph_runtime.get_graph", return_value=graph), \
             patch.object(graph, "invoke") as generation:
            result = TestClient(app).get("/api/agent/workflow?thread_id=clarification-test").json()
            generation.assert_not_called()
        by_id = {item["id"]: item for item in result["nodes"]}
        self.assertEqual(by_id["clarification"]["status"], "waiting")
        self.assertEqual(by_id["extract"]["status"], "completed")
        self.assertEqual(by_id["solution"]["status"], "idle")
        self.assertEqual(result["case"]["pending_steps"], ["clarification"])
        self.assertEqual(result["case"]["status"], "waiting for human input")
        self.assertEqual(graph.get_state(config), before, "Visualization must not modify the case")

    def test_real_failed_specialist_is_distinguished_from_pending_work(self):
        graph = multi_agent_graph.build_graph(InMemorySaver())
        config = {"configurable": {"thread_id": "failed-specialist"}}
        graph.update_state(config, {
            "document_type": "brd", "status": "running", "context": {},
            "supervisor_plan": {"tasks": [{"specialist": name} for name in ("solution", "delivery", "commercial", "risk")]},
            "trace": ["supervisor: focused specialist tasks assigned"],
        }, as_node="supervisor")
        waiting = workflow_view.describe_workflow(graph.get_state(config))
        self.assertEqual(next(item for item in waiting["nodes"] if item["id"] == "solution")["status"], "pending")
        with patch("multi_agent_graph.solution_specialist", side_effect=RuntimeError("Provider unavailable")), \
             patch("multi_agent_graph.delivery_specialist", return_value={}), \
             patch("multi_agent_graph.commercial_specialist", return_value={}), \
             patch("multi_agent_graph.risk_specialist", return_value={}):
            with self.assertRaises(RuntimeError):
                graph.invoke(None, config)
        result = workflow_view.describe_workflow(graph.get_state(config))
        by_id = {item["id"]: item for item in result["nodes"]}
        self.assertEqual(by_id["solution"]["status"], "failed")
        self.assertEqual(by_id["write_brd"]["status"], "idle")

    def test_cancel_does_not_mark_the_export_branch_as_completed(self):
        graph = multi_agent_graph.build_graph(InMemorySaver())
        config = {"configurable": {"thread_id": "cancelled-case"}}
        graph.update_state(config, {"document_type": "proposal", "status": "cancelled",
                                    "trace": ["complete: cancelled by reviewer"]}, as_node="cancel")
        result = workflow_view.describe_workflow(graph.get_state(config))
        overview = {item["id"]: item for item in result["overview_nodes"]}
        self.assertEqual(overview["cancel"]["status"], "completed")
        self.assertEqual(overview["export"]["status"], "idle")

    def test_unknown_thread_and_empty_thread_are_clear_errors(self):
        graph = multi_agent_graph.build_graph(InMemorySaver())
        with patch("api.index.graph_runtime.get_graph", return_value=graph):
            client = TestClient(app)
            self.assertEqual(client.get("/api/agent/workflow?thread_id=missing").status_code, 404)
            self.assertEqual(client.get("/api/agent/workflow?thread_id=").status_code, 400)
            self.assertEqual(client.get("/workflow").status_code, 200)


if __name__ == "__main__":
    unittest.main()
