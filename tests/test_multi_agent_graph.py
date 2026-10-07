import unittest
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import multi_agent_graph


ANALYSIS = {
    "summary": "A CRM requirement.",
    "requirement_sections": [{"product": "Zoho CRM", "requirements": ["Lead management"]}],
    "questions": [], "commercial_categories": [], "duration_estimates": [],
    "context_record_ids": [], "context_sources": [], "comparisons": [],
}


class MultiAgentGraphTests(unittest.TestCase):
    def test_parallel_specialists_reconcile_before_writing(self):
        inventory = {"summary": "A CRM requirement.",
                     "requirement_sections": ANALYSIS["requirement_sections"],
                     "commercial_categories": []}
        evidence = {"products": [], "payload": "{}", "relevant_records": [],
                    "relevant_projects": [], "delivery_records": []}
        proposal = {
            "client": {"company_name": "Example Client", "project_name": "CRM", "contact": ""},
            "project_introduction": "Implement Zoho CRM.",
            "scope": [{"product": "Zoho CRM", "areas": [{"area": "Included work", "tasks": ["Lead management"]}]}],
            "prerequisites": [], "deliverables": [], "open_points": [],
            "timeline": None, "commercials": None, "status": "DRAFT",
        }
        with patch("multi_agent_graph.workflow.extract_inventory", return_value=inventory), \
             patch("multi_agent_graph.supervisor_specialist", return_value={
                 "tasks": [{"specialist": name, "focus": "Review source", "requirement_ids": []}
                           for name in ("solution", "delivery", "commercial", "risk")], "quality_focus": []}), \
             patch("multi_agent_graph.workflow.retrieve_evidence", return_value=evidence), \
             patch("multi_agent_graph.workflow.review_evidence", return_value={"summary": "A CRM requirement.", "questions": []}), \
             patch("multi_agent_graph.workflow.assemble_analysis", return_value=ANALYSIS), \
             patch("multi_agent_graph.solution_specialist", return_value={"agent": "solution_architect", "solutions": []}), \
             patch("multi_agent_graph.delivery_specialist", return_value={"agent": "delivery_estimator", "timeline": {}}), \
             patch("multi_agent_graph.commercial_specialist", return_value={"agent": "commercial_analyst", "commercials": {"items": []}}), \
             patch("multi_agent_graph.risk_specialist", return_value={"agent": "risk_and_dependency", "risks": []}), \
             patch("multi_agent_graph.workflow.prepare_draft", return_value=("prepared", "reference")), \
             patch("multi_agent_graph.brand.draft_proposal", return_value=proposal), \
             patch("multi_agent_graph.workflow.apply_actual_delivery_timeline", side_effect=lambda p, a, q: p), \
             patch("multi_agent_graph.workflow.apply_requested_cost_breakdown", side_effect=lambda p, a: p), \
             patch("multi_agent_graph.finalize_client_content", side_effect=lambda p, a, q, r: p):
            graph = multi_agent_graph.build_graph(checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": "multi-agent-test"}}
            state = graph.invoke({
                "mode": "case", "document_type": "proposal", "requirement": "Lead management",
                "source": "test.txt", "completed": [], "project_data": [], "crm": "",
                "trace": [], "repair_count": 0,
            }, config=config)
            self.assertEqual(state["__interrupt__"][0].value["stage"], "clarification")

            with patch("multi_agent_graph.critic_specialist", return_value={"passed": True, "issues": []}) as critic:
                state = graph.invoke(Command(resume={"answers": {}}), config=config)
                self.assertEqual(state["__interrupt__"][0].value["stage"], "draft_review")
                self.assertEqual(state["trace"].count("supervisor: specialist findings reconciled"), 1)
                state = graph.invoke(Command(resume={"decision": "accept", "reviewer": "Test Reviewer"}), config=config)
                self.assertEqual(critic.call_count, 1, "Unchanged human approval must not re-run the model critic")
            trace = state["trace"]
            for name in ("solution_architect", "delivery_estimator", "commercial_analyst", "risk_agent"):
                self.assertTrue(any(name in item for item in trace), name)

            self.assertEqual(state["status"], "reviewed")
            self.assertEqual(set(state["specialist_findings"]), {"solution", "delivery", "commercial", "risk"})
            self.assertTrue(state["proposal"]["agent_audit"]["quality_report"]["passed"])


if __name__ == "__main__":
    unittest.main()
