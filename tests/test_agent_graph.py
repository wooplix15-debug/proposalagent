import unittest
from unittest.mock import patch

import agent_graph
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command


ANALYSIS = {
    "summary": "A CRM requirement.",
    "requirement_sections": [{"product": "Zoho CRM", "requirements": ["Lead management"]}],
    "questions": [],
    "commercial_categories": [],
    "duration_estimates": [],
    "context_record_ids": [],
    "context_sources": [],
    "comparisons": [],
}


class AgentGraphTests(unittest.TestCase):
    def test_analysis_graph_uses_named_stages(self):
        inventory = {"summary": "A CRM requirement.",
                     "requirement_sections": ANALYSIS["requirement_sections"],
                     "commercial_categories": []}
        evidence = {"products": [], "payload": "{}", "relevant_records": [],
                    "relevant_projects": [], "delivery_records": []}
        with patch("agent_graph.workflow.extract_inventory", return_value=inventory), \
             patch("agent_graph.workflow.retrieve_evidence", return_value=evidence), \
             patch("agent_graph.workflow.review_evidence", return_value={"summary": "A CRM requirement.", "questions": []}), \
             patch("agent_graph.workflow.assemble_analysis", return_value=ANALYSIS):
            result, trace = agent_graph.run_analysis("Lead management", [], "", [])

        self.assertEqual(result["summary"], "A CRM requirement.")
        self.assertIn("extract: source-only checklist extracted", trace)
        self.assertIn("retrieve: relevant approved delivery records selected", trace)
        self.assertIn("complete: analysis_ready", trace)

    def test_proposal_graph_validates_and_returns_draft(self):
        context = {
            "requirement": "Zoho CRM lead management",
            "source": "requirement.txt",
            "analysis": ANALYSIS,
        }
        proposal = {
            "client": {"company_name": "Example Client", "project_name": "CRM", "contact": ""},
            "project_introduction": "Implement Zoho CRM.",
            "scope": [{"product": "Zoho CRM", "areas": [{"area": "Included work", "tasks": ["Lead management"]}]}],
            "prerequisites": [], "deliverables": [], "open_points": [],
            "timeline": None, "commercials": None, "status": "DRAFT",
        }
        with patch("agent_graph.workflow.prepare_draft", return_value=("prepared", "reference")), \
             patch("agent_graph.brand.draft_proposal", return_value=proposal), \
             patch("agent_graph.workflow.apply_actual_delivery_timeline", side_effect=lambda p, a, q: p), \
             patch("agent_graph.workflow.apply_requested_cost_breakdown", side_effect=lambda p, a: p), \
             patch("agent_graph.finalize_client_content", side_effect=lambda p, a, q, r: p):
            result, trace = agent_graph.run_proposal(context, {})

        self.assertEqual(result["client"]["company_name"], "Example Client")
        self.assertIn("draft_proposal: structured proposal created", trace)
        self.assertIn("validate: passed", trace)

    def test_brd_validation_rejects_missing_source_requirement(self):
        context = {"requirement": "Zoho CRM lead management", "analysis": ANALYSIS}
        document = {
            "title": "Business Requirements Document", "project_name": "CRM",
            "status": "Draft for business review", "requirements": [],
            "specification_tables": [], "summary": "Summary", "areas": [],
            "requirements_by_area": [], "process_views": [], "current_state": [],
            "objectives": [], "stakeholders": [], "training_requirements": [],
            "open_decisions": [], "acceptance_criteria": [], "prepared_by": "Wooplix",
            "date": "7th Oct, 2026",
        }
        validation = agent_graph.validate_output("brd", document, context)
        self.assertFalse(validation["passed"])
        self.assertIn("The BRD contains no traceable requirements.", validation["issues"])

    def test_case_graph_pauses_for_clarification_and_review(self):
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
        with patch("agent_graph.workflow.extract_inventory", return_value=inventory), \
             patch("agent_graph.workflow.retrieve_evidence", return_value=evidence), \
             patch("agent_graph.workflow.review_evidence", return_value={"summary": "A CRM requirement.", "questions": []}), \
             patch("agent_graph.workflow.assemble_analysis", return_value=ANALYSIS), \
             patch("agent_graph.workflow.prepare_draft", return_value=("prepared", "reference")), \
             patch("agent_graph.brand.draft_proposal", return_value=proposal), \
             patch("agent_graph.workflow.apply_actual_delivery_timeline", side_effect=lambda p, a, q: p), \
             patch("agent_graph.workflow.apply_requested_cost_breakdown", side_effect=lambda p, a: p), \
             patch("agent_graph.finalize_client_content", side_effect=lambda p, a, q, r: p):
            graph = agent_graph.build_graph(checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": "test-case-1"}}
            state = graph.invoke({
                "mode": "case", "document_type": "proposal", "requirement": "Lead management",
                "source": "test.txt", "completed": [], "project_data": [], "crm": "",
                "trace": [], "repair_count": 0,
            }, config=config)
            self.assertIn("__interrupt__", state)
            self.assertEqual(state["__interrupt__"][0].value["stage"], "clarification")

            state = graph.invoke(Command(resume={"answers": {}}), config=config)
            self.assertEqual(state["__interrupt__"][0].value["stage"], "draft_review")

            state = graph.invoke(Command(resume={
                "decision": "accept", "reviewer": "Test Reviewer"
            }), config=config)
            self.assertEqual(state["status"], "reviewed")
            self.assertIn("human_review: accept", state["trace"])


if __name__ == "__main__":
    unittest.main()
