"""Bounded multi-agent LangGraph workflow for Proposal and BRD cases."""
from __future__ import annotations

import copy
import hashlib
import json
import operator
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

import agent_graph as base_graph
import business_requirements_agent as brd_agent
import proposal_workflow as workflow
import wooplix_agent as brand
from graph_schemas import Analysis, DraftReviewResponse, validate_answers
from multi_agent_specialists import (
    commercial_specialist,
    critic_specialist,
    delivery_specialist,
    repair_specialist,
    risk_specialist,
    solution_specialist,
    supervisor_specialist,
)
from multi_agent_tools import (
    apply_text_patches, attach_findings, check_references, critic_batches, requirement_register,
)
from proposal_scope import confirmed_scope, finalize_client_content


class MultiAgentState(TypedDict, total=False):
    mode: Literal["case"]
    document_type: Literal["proposal", "brd"]
    requirement: str
    source: str
    completed: list[dict[str, Any]]
    project_data: list[dict[str, Any]]
    crm: str
    inventory: dict[str, Any]
    evidence: dict[str, Any]
    evidence_review: dict[str, Any]
    context: dict[str, Any]
    analysis: dict[str, Any]
    answers: dict[str, str]
    supervisor_plan: dict[str, Any]
    solution_findings: dict[str, Any]
    delivery_findings: dict[str, Any]
    commercial_findings: dict[str, Any]
    risk_findings: dict[str, Any]
    specialist_findings: dict[str, Any]
    prepared_requirement: str
    reference: str
    proposal: dict[str, Any]
    document: dict[str, Any]
    validation: dict[str, Any]
    repair_count: int
    review_decision: str
    reviewer: str
    reviewed: bool
    status: str
    precomputed_context: bool
    require_human_review: bool
    requirement_register: list[dict[str, Any]]
    quality_report: dict[str, Any]
    critic_digest: str
    critic_packets: list[list[dict[str, str]]]
    critic_cursor: int
    critic_issues: list[dict[str, Any]]
    trace: Annotated[list[str], operator.add]


def _trace(message):
    return {"trace": [message]}


def _intake(state):
    if state.get("mode") != "case" or state.get("document_type") not in {"proposal", "brd"}:
        raise ValueError("The multi-agent graph requires a proposal or brd case.")
    requirement = state.get("requirement", "").strip()
    if not requirement:
        raise ValueError("Supply a non-empty requirement.")
    if len(requirement) > 60000:
        raise ValueError("Shorten the requirement to 60,000 characters.")
    return {"status": "running", **_trace("supervisor: case accepted")}


def _extract(state):
    inventory = workflow.extract_inventory(state["requirement"])
    return {"inventory": inventory, **_trace("requirement_agent: source checklist extracted")}


def _retrieve(state):
    evidence = workflow.retrieve_evidence(
        state["requirement"], state.get("completed", []), state.get("crm", "")
    )
    return {"evidence": evidence, **_trace("retrieval_tool: approved evidence selected")}


def _review_evidence(state):
    review = workflow.review_evidence(state["evidence"])
    return {"evidence_review": review,
            **_trace("requirement_agent: evidence and clarification gaps reviewed")}


def _assemble(state):
    analysis = workflow.assemble_analysis(
        state["requirement"], state["inventory"], state["evidence"],
        state["evidence_review"], state.get("project_data", [])
    )
    analysis = Analysis.model_validate(analysis).model_dump(mode="json")
    ids = set(analysis.get("context_record_ids", []))
    ids.update(row["record_id"] for row in analysis.get("duration_estimates", []) if row.get("record_id"))
    sources = set(analysis.get("context_sources", []))
    context = {
        "requirement": state["requirement"],
        "source": state.get("source", "Pasted requirement"),
        "analysis": analysis,
        "completed": [row for row in state.get("completed", [])
                      if row.get("record_id") in ids or (not row.get("record_id") and row.get("source") in sources)],
        "timing_sources": [row for row in state.get("project_data", []) if row.get("record_id") in ids],
        "crm": state.get("crm", ""),
    }
    return {"analysis": analysis, "context": context,
            **_trace("supervisor: canonical requirement state assembled")}


def _clarification(state):
    response = interrupt({
        "stage": "clarification",
        "source": state.get("source", "Pasted requirement"),
        "summary": state["analysis"]["summary"],
        "requirement_sections": state["analysis"]["requirement_sections"],
        "questions": state["analysis"]["questions"],
        "duration_estimates": state["analysis"]["duration_estimates"],
        "instruction": "Review scope and submit answers; use Leave open for discovery for unknowns.",
    })
    answers = validate_answers(state["analysis"], response.get("answers", {}))
    return {"answers": answers, **_trace("human: clarification answers received")}


def _supervisor(state):
    context = copy.deepcopy(state["context"])
    Analysis.model_validate(context["analysis"])
    context["answers"] = validate_answers(context["analysis"], state.get("answers", {}))
    register = requirement_register(context)
    if not register:
        raise ValueError("No source requirements are available for specialist analysis.")
    context["requirement_register"] = register
    plan = supervisor_specialist(context, state["document_type"])
    return {"supervisor_plan": plan, "context": context, "requirement_register": register,
            **_trace("supervisor: focused specialist tasks assigned")}


def _task(state, name):
    return next(task for task in state["supervisor_plan"]["tasks"] if task["specialist"] == name)


def _solution(state):
    return {"solution_findings": solution_specialist(state["context"], _task(state, "solution")),
            **_trace("solution_architect: solution mapping completed")}


def _delivery(state):
    return {"delivery_findings": delivery_specialist(state["context"], _task(state, "delivery")),
            **_trace("delivery_estimator: evidence-based timeline calculated")}


def _commercial(state):
    return {"commercial_findings": commercial_specialist(state["context"], _task(state, "commercial")),
            **_trace("commercial_analyst: requested commercial categories preserved")}


def _risk(state):
    return {"risk_findings": risk_specialist(state["context"], _task(state, "risk")),
            **_trace("risk_agent: open risks and dependencies identified")}


def _reconcile(state):
    findings = {
        "solution": state["solution_findings"],
        "delivery": state["delivery_findings"],
        "commercial": state["commercial_findings"],
        "risk": state["risk_findings"],
    }
    for name, report in findings.items():
        # Numeric estimates are tool-produced, not generated planning prose.
        grounded = {key: value for key, value in report.items() if key not in {"timeline", "commercials", "agent"}}
        check_references(grounded, state["context"])
    analysis = copy.deepcopy(state["analysis"])
    analysis["specialist_findings"] = findings
    context = copy.deepcopy(state["context"])
    context["analysis"] = analysis
    context["specialist_findings"] = findings
    return {"analysis": analysis, "context": context, "specialist_findings": findings,
            **_trace("supervisor: specialist findings reconciled")}


def _prepare(state):
    context = state["context"]
    answers = validate_answers(context["analysis"], state.get("answers", {}))
    prepared, reference = workflow.prepare_draft(context, answers)
    reference += "\nSPECIALIST FINDINGS (planning evidence; do not print as internal metadata)\n"
    reference += json.dumps(state["specialist_findings"], ensure_ascii=False)
    return {"prepared_requirement": prepared, "reference": reference,
            **_trace("writer: reviewed requirement and specialist evidence prepared")}


def _write_proposal(state):
    proposal = brand.draft_proposal(
        state["prepared_requirement"], state["reference"],
        state["analysis"].get("requirement_sections", [])
    )
    return {"proposal": proposal, **_trace("proposal_writer: proposal draft created")}


def _enrich_proposal(state):
    proposal = copy.deepcopy(state["proposal"])
    analysis = state["analysis"]
    proposal["timeline"] = copy.deepcopy(state["delivery_findings"]["timeline"])
    proposal["commercials"] = copy.deepcopy(state["commercial_findings"]["commercials"])
    proposal = finalize_client_content(proposal, analysis, state.get("answers", {}), state["prepared_requirement"])
    proposal["status"] = "DRAFT"
    metadata = state["supervisor_plan"].get("project_metadata", {})
    client = proposal.setdefault("client", {})
    for key, value in metadata.items():
        if value:
            client[key] = value
    proposal = attach_findings(proposal, state["specialist_findings"], state["requirement_register"])
    return {"proposal": proposal, **_trace("proposal_writer: deterministic findings applied")}


def _write_brd(state):
    document = brd_agent.build_document(
        state["context"]["requirement"], state["analysis"], state.get("answers", {}),
        source_name=state["context"].get("source", "")
    )
    document["status"] = "Draft for business review"
    metadata = state["supervisor_plan"].get("project_metadata", {})
    if metadata.get("company_name"):
        document["prepared_for"] = metadata["company_name"]
    if metadata.get("project_name"):
        document["project_name"] = metadata["project_name"]
    document = attach_findings(document, state["specialist_findings"], state["requirement_register"])
    return {"document": document, **_trace("brd_writer: BRD draft created")}


def _validate(state):
    kind = state["document_type"]
    document = state["proposal"] if kind == "proposal" else state["document"]
    result = base_graph.validate_output(kind, document, state["context"], state.get("prepared_requirement", ""))
    return {"validation": result,
            **_trace(f"validation_tool: {'passed' if result['passed'] else 'failed'}")}


def _critic(state):
    document = state["proposal"] if state["document_type"] == "proposal" else state["document"]
    draft = {key: value for key, value in document.items() if key != "agent_audit"}
    digest = hashlib.sha256(json.dumps(draft, sort_keys=True).encode()).hexdigest()
    if digest == state.get("critic_digest"):
        return {"critic_packets": [], **_trace("quality_critic: unchanged reviewed draft retains its quality check")}
    packets = critic_batches(document)
    return {"critic_packets": packets, "critic_cursor": 0, "critic_issues": [],
            "critic_digest": digest, **_trace(f"quality_critic: {len(packets)} bounded review packets prepared")}


def _critic_packet(state):
    document = state["proposal"] if state["document_type"] == "proposal" else state["document"]
    cursor = state["critic_cursor"]
    report = critic_specialist(state["context"], state["document_type"], document,
                               state["supervisor_plan"]["quality_focus"], state["critic_packets"][cursor])
    issues = state["critic_issues"] + report["issues"]
    # Deduplicate repeated claims spanning adjacent packets.
    unique = {(issue["path"], issue["excerpt"], issue["category"]): issue for issue in issues}
    quality = {"issues": list(unique.values()),
               "passed": not any(issue["severity"] == "blocking" for issue in unique.values())}
    return {"critic_cursor": cursor + 1, "critic_issues": quality["issues"], "quality_report": quality,
            **_trace(f"quality_critic: packet {cursor + 1} of {len(state['critic_packets'])} checked")}


def _after_critic_start(state):
    return "critic_packet" if state["critic_packets"] else _after_critic(state)


def _after_critic_packet(state):
    return "critic_packet" if state["critic_cursor"] < len(state["critic_packets"]) else _after_critic(state)


def _repair(state):
    kind = state["document_type"]
    document = copy.deepcopy(state["proposal"] if kind == "proposal" else state["document"])
    if not state["validation"]["passed"]:
        if kind != "proposal":
            raise ValueError("An invalid BRD structure cannot be repaired through prose edits.")
        document["scope"] = confirmed_scope(state["analysis"]["requirement_sections"], state["prepared_requirement"])
    else:
        patches = repair_specialist(state["context"], document, state["quality_report"])
        document = apply_text_patches(document, patches["patches"])
    return {"proposal" if kind == "proposal" else "document": document,
            "repair_count": state.get("repair_count", 0) + 1,
            **_trace("revision_writer: one bounded targeted repair applied")}


def _review(state):
    kind = state["document_type"]
    document = state["proposal"] if kind == "proposal" else state["document"]
    response = interrupt({
        "stage": "draft_review", "document_type": kind, "document": document,
        "specialist_findings": state["specialist_findings"],
        "quality_report": state["quality_report"],
        "instruction": "Review the multi-agent draft. Accept, provide an edited document, or cancel.",
    })
    decision = DraftReviewResponse.model_validate(response)
    result = {"review_decision": decision.decision, "reviewer": decision.reviewer}
    if decision.decision == "accept":
        result["proposal" if kind == "proposal" else "document"] = (
            decision.edited_document if decision.edited_document is not None else document
        )
        result["reviewed"] = True
    return {**result, **_trace(f"human: draft review {decision.decision}")}


def _finish(state):
    key = "proposal" if state["document_type"] == "proposal" else "document"
    document = copy.deepcopy(state[key])
    document.setdefault("agent_audit", {
        "specialist_findings": state["specialist_findings"],
        "requirement_register": state["requirement_register"],
    })
    document["agent_audit"]["quality_report"] = state["quality_report"]
    return {key: document, "status": "reviewed" if state.get("reviewed") else "draft_ready",
            **_trace("complete: reviewed" if state.get("reviewed") else "complete: draft ready")}


def _cancel(state):
    return {"status": "cancelled", **_trace("complete: cancelled by reviewer")}


def _after_validation(state):
    if not state["validation"]["passed"]:
        return "repair" if state["document_type"] == "proposal" and state.get("repair_count", 0) < 1 and not state.get("reviewed") else "reject"
    return "critic"


def _after_critic(state):
    if not state["quality_report"]["passed"]:
        return "repair" if state.get("repair_count", 0) < 1 and not state.get("reviewed") else "reject"
    if not state.get("require_human_review", True):
        return "finish"
    return "review" if not state.get("reviewed") else "finish"


def _after_intake(state):
    return "supervisor" if state.get("precomputed_context") else "extract"


def _reject(state):
    issues = list(state["validation"]["issues"])
    issues += [row["reason"] for row in state.get("quality_report", {}).get("issues", [])
               if row["severity"] == "blocking"]
    raise base_graph.AgentGraphError("Quality review rejected the draft: " + "; ".join(issues))


def build_graph(checkpointer=None):
    graph = StateGraph(MultiAgentState)
    nodes = {
        "intake": _intake, "extract": _extract, "retrieve": _retrieve,
        "review_evidence": _review_evidence, "assemble": _assemble,
        "clarification": _clarification, "supervisor": _supervisor,
        "solution": _solution, "delivery": _delivery, "commercial": _commercial,
        "risk": _risk, "reconcile": _reconcile, "prepare": _prepare,
        "write_proposal": _write_proposal, "enrich_proposal": _enrich_proposal,
        "write_brd": _write_brd, "validate": _validate, "critic": _critic,
        "critic_packet": _critic_packet, "repair": _repair,
        "reject": _reject, "review": _review, "finish": _finish, "cancel": _cancel,
    }
    for name, node in nodes.items():
        graph.add_node(name, node)
    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", _after_intake, ["extract", "supervisor"])
    graph.add_edge("extract", "retrieve")
    graph.add_edge("retrieve", "review_evidence")
    graph.add_edge("review_evidence", "assemble")
    graph.add_edge("assemble", "clarification")
    graph.add_edge("clarification", "supervisor")
    for specialist in ("solution", "delivery", "commercial", "risk"):
        graph.add_edge("supervisor", specialist)
    graph.add_edge(["solution", "delivery", "commercial", "risk"], "reconcile")
    graph.add_edge("reconcile", "prepare")
    graph.add_conditional_edges("prepare", lambda state: state["document_type"],
                                {"proposal": "write_proposal", "brd": "write_brd"})
    graph.add_edge("write_proposal", "enrich_proposal")
    graph.add_edge("enrich_proposal", "validate")
    graph.add_edge("write_brd", "validate")
    graph.add_conditional_edges("validate", _after_validation,
                                {"repair": "repair", "reject": "reject", "critic": "critic"})
    graph.add_conditional_edges("critic", _after_critic_start, ["critic_packet", "repair", "reject", "review", "finish"])
    graph.add_conditional_edges("critic_packet", _after_critic_packet,
                                ["critic_packet", "repair", "reject", "review", "finish"])
    graph.add_edge("repair", "validate")
    graph.add_conditional_edges("review", lambda state: state["review_decision"],
                                {"accept": "validate", "cancel": "cancel"})
    graph.add_edge("finish", END)
    graph.add_edge("cancel", END)
    graph.add_edge("reject", END)
    return graph.compile(checkpointer=checkpointer)


_STATELESS_GRAPH = build_graph()


def run_proposal(context, answers):
    """Run specialist agents for the existing two-step browser API."""
    result = _STATELESS_GRAPH.invoke({
        "mode": "case", "document_type": "proposal",
        "requirement": context["requirement"], "source": context.get("source", ""),
        "context": context, "analysis": context["analysis"], "answers": answers,
        "precomputed_context": True, "require_human_review": False,
        "trace": [], "repair_count": 0,
    }, {"recursion_limit": 60})
    return result["proposal"], result.get("trace", [])


def run_brd(context, answers):
    """Run specialist agents for the existing two-step BRD API."""
    result = _STATELESS_GRAPH.invoke({
        "mode": "case", "document_type": "brd",
        "requirement": context["requirement"], "source": context.get("source", ""),
        "context": context, "analysis": context["analysis"], "answers": answers,
        "precomputed_context": True, "require_human_review": False,
        "trace": [], "repair_count": 0,
    }, {"recursion_limit": 60})
    return result["document"], result.get("trace", [])
