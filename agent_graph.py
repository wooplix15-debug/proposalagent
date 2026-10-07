"""LangGraph state machine with source grounding and human-review interrupts.

Stateless entry points keep the existing web/CLI contract. The persistent
case graph additionally pauses for clarification and draft review, using a
caller-supplied checkpointer. All state is JSON-compatible.
"""
from __future__ import annotations

import copy
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

import business_requirements_agent as brd_agent
import proposal_workflow as workflow
import wooplix_agent as brand
from graph_schemas import Analysis, BRD, DraftReviewResponse, Proposal, validate_answers
from proposal_scope import confirmed_scope, coverage_gaps, finalize_client_content, unsupported_scope


class AgentState(TypedDict, total=False):
    mode: Literal["analysis", "proposal", "brd", "case"]
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
    answers: dict[str, str]
    analysis: dict[str, Any]
    prepared_requirement: str
    reference: str
    proposal: dict[str, Any]
    document: dict[str, Any]
    trace: list[str]
    validation: dict[str, Any]
    repair_count: int
    review_decision: str
    reviewer: str
    reviewed: bool
    status: str


class AgentGraphError(RuntimeError):
    """The graph failed its output validation and cannot release a draft."""


def _record(state, message):
    return {"trace": [*state.get("trace", []), message]}


def _document_type(state):
    return state["document_type"] if state["mode"] == "case" else state["mode"]


def _intake(state):
    mode = state.get("mode")
    if mode not in {"analysis", "proposal", "brd", "case"}:
        raise ValueError("Choose analysis, proposal, BRD or case graph mode.")
    if mode == "case" and state.get("document_type") not in {"proposal", "brd"}:
        raise ValueError("Choose proposal or BRD for the case.")
    requirement = (state.get("context") or {}).get("requirement", state.get("requirement", ""))
    if not isinstance(requirement, str) or not requirement.strip():
        raise ValueError("Supply a non-empty requirement.")
    if len(requirement) > 60000:
        raise ValueError("Shorten the requirement to 60,000 characters.")
    return dict(_record(state, f"intake: {mode} route selected"), status="running")


def _extract(state):
    inventory = workflow.extract_inventory(state["requirement"])
    return dict(_record(state, "extract: source-only checklist extracted"), inventory=inventory)


def _retrieve(state):
    evidence = workflow.retrieve_evidence(
        state["requirement"], state.get("completed", []), state.get("crm", "")
    )
    return dict(_record(state, "retrieve: relevant approved delivery records selected"), evidence=evidence)


def _review_evidence(state):
    result = workflow.review_evidence(state["evidence"])
    return dict(_record(state, "review_evidence: timing references and questions evaluated"),
                evidence_review=result)


def _assemble(state):
    analysis = workflow.assemble_analysis(
        state["requirement"], state["inventory"], state["evidence"],
        state["evidence_review"], state.get("project_data", [])
    )
    analysis = Analysis.model_validate(analysis).model_dump(mode="json")
    ids = set(analysis.get("context_record_ids", []))
    sources = set(analysis.get("context_sources", []))
    ids.update(row["record_id"] for row in analysis["duration_estimates"] if row.get("record_id"))
    context = {
        "requirement": state["requirement"],
        "source": state.get("source", "Pasted requirement"),
        "analysis": analysis,
        "completed": [row for row in state.get("completed", [])
                      if row.get("record_id") in ids or (not row.get("record_id") and row.get("source") in sources)],
        "timing_sources": [row for row in state.get("project_data", []) if row.get("record_id") in ids],
        "crm": state.get("crm", ""),
    }
    return dict(_record(state, "assemble: source coverage and analysis schema validated"),
                analysis=analysis, context=context)


def _clarification(state):
    # No model call or other side effect before the interrupt: resuming this
    # node must not repeat extraction/retrieval or incur additional model cost.
    response = interrupt({
        "stage": "clarification",
        "summary": state["analysis"]["summary"],
        "requirement_sections": state["analysis"]["requirement_sections"],
        "questions": state["analysis"]["questions"],
        "duration_estimates": state["analysis"]["duration_estimates"],
        "instruction": "Review scope and submit answers; use Leave open for discovery for unknowns.",
    })
    if not isinstance(response, dict):
        raise ValueError("Submit an object containing answers.")
    answers = validate_answers(state["analysis"], response.get("answers", {}))
    return dict(_record(state, "clarification: human answers received"), answers=answers)


def _prepare(state):
    context = state["context"]
    Analysis.model_validate(context["analysis"])
    answers = validate_answers(context["analysis"], state.get("answers", {}))
    requirement, reference = workflow.prepare_draft(context, answers)
    return dict(_record(state, "prepare: reviewed answers attached once"),
                prepared_requirement=requirement, reference=reference, answers=answers)


def _draft_proposal(state):
    proposal = brand.draft_proposal(
        state["prepared_requirement"], state["reference"],
        state["context"]["analysis"].get("requirement_sections", [])
    )
    return dict(_record(state, "draft_proposal: structured proposal created"), proposal=proposal)


def _enrich(state):
    proposal = copy.deepcopy(state["proposal"])
    analysis = state["context"]["analysis"]
    answers = state["answers"]
    proposal = workflow.apply_actual_delivery_timeline(proposal, analysis, answers)
    proposal = workflow.apply_requested_cost_breakdown(proposal, analysis)
    proposal = finalize_client_content(proposal, analysis, answers, state["prepared_requirement"])
    # Release/approval status is a code-controlled field, not a model decision.
    proposal["status"] = "DRAFT"
    return dict(_record(state, "enrich: deterministic timeline and commercial rules applied"), proposal=proposal)


def _draft_brd(state):
    context = state["context"]
    document = brd_agent.build_document(
        context["requirement"], context["analysis"], state["answers"],
        source_name=context.get("source", "")
    )
    document["status"] = "Draft for business review"
    return dict(_record(state, "draft_brd: source-grounded BRD created"), document=document)


def validate_output(document_type, document, context, prepared_requirement=""):
    """Validate an AI or human-edited document using the same export contract."""
    issues = []
    try:
        if document_type == "proposal":
            Proposal.model_validate(document)
            sections = context["analysis"].get("requirement_sections", [])
            issues.extend(coverage_gaps(document, sections))
            issues.extend(unsupported_scope(document, prepared_requirement or context["requirement"]))
        else:
            BRD.model_validate(document)
            requirements = document.get("requirements", [])
            if not requirements:
                issues.append("The BRD contains no traceable requirements.")
            if not document.get("specification_tables"):
                issues.append("The BRD contains no specification tables.")
            ids = [row["id"] for row in requirements]
            if len(ids) != len(set(ids)):
                issues.append("BRD requirement IDs are not unique.")
            for table in document.get("specification_tables", []):
                if not set(table.get("requirement_ids", [])).issubset(set(ids)):
                    issues.append("A BRD table references an unknown requirement ID.")
            expected = brd_agent._section_items(brd_agent._combine_explicit_shared_areas(
                context["requirement"], context["analysis"].get("requirement_sections", [])
            ))
            # The source BRD builder excludes qualified future scope. Compare
            # only the requirements that the original builder commits to.
            actual = {(r["area"].casefold(), r["requirement"].casefold()) for r in requirements}
            import re
            for row in expected:
                if re.search(r"\b(?:not required (?:in|for|during) (?:the |this |current )?phase|out of scope|excluded from (?:this |the |current )?scope|future phase|future scope|future enhancement|optional feature)\b", row["requirement"], re.I):
                    continue
                if (row["area"].casefold(), row["requirement"].casefold()) not in actual:
                    issues.append("The BRD omitted a source requirement: " + row["id"])
    except (ValidationError, KeyError, TypeError) as exc:
        # Do not echo confidential cell values in validation errors/logs.
        if isinstance(exc, ValidationError):
            issues.extend("Invalid field: " + ".".join(str(x) for x in error["loc"])
                          for error in exc.errors(include_input=False))
        else:
            issues.append("The document structure is incomplete.")
    return {"passed": not issues, "issues": list(dict.fromkeys(issues))}


def _validate(state):
    kind = _document_type(state)
    document = state["proposal"] if kind == "proposal" else state["document"]
    result = validate_output(kind, document, state["context"], state["prepared_requirement"])
    return dict(_record(state, f"validate: {'passed' if result['passed'] else 'failed'}"), validation=result)


def _repair(state):
    # Only one source-grounded repair; no open-ended AI correction loop.
    proposal = copy.deepcopy(state["proposal"])
    proposal["scope"] = confirmed_scope(
        state["context"]["analysis"]["requirement_sections"], state["prepared_requirement"]
    )
    return dict(_record(state, "repair: scope rebuilt from the validated source checklist"),
                proposal=proposal, repair_count=state.get("repair_count", 0) + 1)


def _reject(state):
    raise AgentGraphError("Document validation failed: " + "; ".join(state["validation"]["issues"]))


def _human_review(state):
    kind = _document_type(state)
    document = state["proposal"] if kind == "proposal" else state["document"]
    raw = interrupt({
        "stage": "draft_review",
        "document_type": kind,
        "document": document,
        "instruction": "Review this draft. Accept, submit an edited document, or cancel.",
    })
    response = DraftReviewResponse.model_validate(raw)
    result = dict(_record(state, f"human_review: {response.decision}"),
                  review_decision=response.decision, reviewer=response.reviewer)
    if response.decision == "accept":
        edited = response.edited_document or document
        result["proposal" if kind == "proposal" else "document"] = edited
        result["reviewed"] = True
    return result


def _finish(state):
    status = "analysis_ready" if state["mode"] == "analysis" else (
        "reviewed" if state.get("reviewed") else "draft_ready"
    )
    return dict(_record(state, f"complete: {status}"), status=status)


def _cancel(state):
    return dict(_record(state, "complete: cancelled by reviewer"), status="cancelled")


def _after_intake(state):
    return "extract" if state["mode"] in {"analysis", "case"} else "prepare"


def _after_analysis(state):
    return "clarification" if state["mode"] == "case" else "finish"


def _after_validation(state):
    if not state["validation"]["passed"]:
        if _document_type(state) == "proposal" and state.get("repair_count", 0) < 1 and not state.get("reviewed"):
            return "repair"
        return "reject"
    if state["mode"] == "case" and not state.get("reviewed"):
        return "human_review"
    return "finish"


def build_graph(checkpointer=None):
    """Compile all bounded routes; persistent cases require a checkpointer."""
    graph = StateGraph(AgentState)
    for name, node in {
        "intake": _intake, "extract": _extract, "retrieve": _retrieve,
        "review_evidence": _review_evidence, "assemble": _assemble,
        "clarification": _clarification, "prepare": _prepare,
        "draft_proposal": _draft_proposal, "enrich": _enrich,
        "draft_brd": _draft_brd, "validate": _validate, "repair": _repair,
        "reject": _reject, "human_review": _human_review,
        "finish": _finish, "cancel": _cancel,
    }.items():
        graph.add_node(name, node)
    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", _after_intake, ["extract", "prepare"])
    graph.add_edge("extract", "retrieve")
    graph.add_edge("retrieve", "review_evidence")
    graph.add_edge("review_evidence", "assemble")
    graph.add_conditional_edges("assemble", _after_analysis, ["clarification", "finish"])
    graph.add_edge("clarification", "prepare")
    graph.add_conditional_edges("prepare", _document_type,
                                {"proposal": "draft_proposal", "brd": "draft_brd"})
    graph.add_edge("draft_proposal", "enrich")
    graph.add_edge("enrich", "validate")
    graph.add_edge("draft_brd", "validate")
    graph.add_conditional_edges("validate", _after_validation, ["repair", "reject", "human_review", "finish"])
    graph.add_edge("repair", "validate")
    graph.add_conditional_edges("human_review", lambda state: state["review_decision"],
                                {"accept": "validate", "cancel": "cancel"})
    graph.add_edge("finish", END)
    graph.add_edge("cancel", END)
    graph.add_edge("reject", END)
    return graph.compile(checkpointer=checkpointer)


_STATELESS_GRAPH = build_graph()


def run_analysis(requirement, completed, crm, project_data):
    result = _STATELESS_GRAPH.invoke({
        "mode": "analysis", "requirement": requirement,
        "completed": completed, "crm": crm, "project_data": project_data,
        "trace": [],
    }, {"recursion_limit": 30})
    return result["analysis"], result["trace"]


def run_proposal(context, answers):
    result = _STATELESS_GRAPH.invoke({"mode": "proposal", "context": context,
                                     "answers": answers, "trace": [], "repair_count": 0},
                                    {"recursion_limit": 30})
    return result["proposal"], result["trace"]


def run_brd(context, answers):
    result = _STATELESS_GRAPH.invoke({"mode": "brd", "context": context,
                                     "answers": answers, "trace": [], "repair_count": 0},
                                    {"recursion_limit": 30})
    return result["document"], result["trace"]
