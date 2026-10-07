"""Display metadata over the actual compiled LangGraph topology."""
import re

import multi_agent_graph


def node(label, kind, x, y, description, role=None, patterns=(), function=None):
    return {"label": label, "kind": kind, "x": x, "y": y, "description": description,
            "role": role, "trace_patterns": list(patterns),
            "source": "multi_agent_graph.py" + (f"::{function}" if function else "")}


NODES = {
    "__start__": node("Start", "control", 740, 20, "A new case enters the graph."),
    "intake": node("Requirement Intake", "control", 740, 160, "Validate the case and select the saved or new-input route.",
                   patterns=[r"supervisor: case accepted"], function="_intake"),
    "extract": node("Requirement Analyst · Extract", "agent", 740, 300, "Extract the source-only requirement checklist.",
                    "requirement_analyst", [r"requirement_agent: source checklist extracted"], "_extract"),
    "retrieve": node("Evidence Retrieval", "tool", 740, 440, "Read relevant bundled delivery records. This is a Python retrieval tool.",
                     patterns=[r"retrieval_tool:"], function="_retrieve"),
    "review_evidence": node("Requirement Analyst · Review", "agent", 740, 580, "Review the evidence and identify clarification gaps. Same analyst role as extraction.",
                            "requirement_analyst", [r"requirement_agent: evidence"], "_review_evidence"),
    "assemble": node("Canonical Requirement State", "tool", 740, 720, "Validate the shared checklist and attach source evidence.",
                     patterns=[r"supervisor: canonical"], function="_assemble"),
    "clarification": node("Human Clarification", "human", 740, 860, "Pause for the user's answers. The graph is saved in SQLite.",
                          patterns=[r"human: clarification"], function="_clarification"),
    "supervisor": node("Supervisor Agent", "agent", 740, 1000, "Assign focused tasks to four specialists and set quality-review priorities.",
                       "supervisor", [r"supervisor: focused specialist"], "_supervisor"),
    "solution": node("Solution Architect", "agent", 140, 1190, "Map requested capabilities to proposed designs and evidence. Does not approve a design.",
                     "solution_architect", [r"solution_architect:"], "_solution"),
    "delivery": node("Delivery Estimator", "agent", 490, 1190, "Identify delivery dependencies. Python calculates the actual duration figures.",
                     "delivery_estimator", [r"delivery_estimator:"], "_delivery"),
    "commercial": node("Commercial Analyst", "agent", 840, 1190, "Identify quote dependencies for requested categories. Without a rate card, amounts remain unquoted.",
                       "commercial_analyst", [r"commercial_analyst:"], "_commercial"),
    "risk": node("Risk & Dependency Agent", "agent", 1190, 1190, "Identify source-linked risks, impacts and mitigations.",
                 "risk_and_dependency", [r"risk_agent:"], "_risk"),
    "reconcile": node("Reconcile Specialist Findings", "tool", 740, 1390, "Wait for all four parallel branches, then validate IDs, evidence and products.",
                      patterns=[r"supervisor: specialist findings reconciled"], function="_reconcile"),
    "prepare": node("Prepare Writer Context", "tool", 740, 1530, "Attach reviewed answers and specialist findings to the writing context.",
                    patterns=[r"writer: reviewed requirement"], function="_prepare"),
    "write_proposal": node("Proposal Writer", "agent", 490, 1710, "Write the proposal from the canonical requirement state and specialist findings.",
                           "proposal_writer", [r"proposal_writer: proposal draft"], "_write_proposal"),
    "write_brd": node("BRD Writer", "agent", 990, 1710, "Develop source-grounded BRD specifications, including fields, workflows, access and reporting.",
                      "brd_writer", [r"brd_writer:"], "_write_brd"),
    "enrich_proposal": node("Apply Timeline & Commercials", "tool", 490, 1860, "Apply tool-calculated timing and commercial data, plus planning and traceability tables.",
                            patterns=[r"proposal_writer: deterministic"], function="_enrich_proposal"),
    "validate": node("Deterministic Validation", "tool", 740, 2040, "Check structure, source coverage, IDs and unsupported scope before model review.",
                     patterns=[r"validation_tool:"], function="_validate"),
    "critic": node("Prepare Quality Review", "tool", 740, 2180, "Split long drafts into small review packets. Reuse the check for an unchanged draft.",
                   patterns=[r"quality_critic: .*prepared", r"quality_critic: unchanged"], function="_critic"),
    "critic_packet": node("Quality Critic", "agent", 740, 2320, "Independently review each packet for unsupported claims or contradictions. Each packet has a checkpoint.",
                          "quality_critic", [r"quality_critic: packet"], "_critic_packet"),
    "repair": node("Revision Writer · Optional", "agent", 1340, 2200, "Make one targeted correction, then validate and review again. Source rows and prices are locked.",
                   "revision_writer", [r"revision_writer:"], "_repair"),
    "reject": node("Reject Draft", "control", 1340, 2380, "Stop a draft that still fails quality checks after the allowed repair.", function="_reject"),
    "review": node("Human Draft Review", "human", 740, 2510, "Accept, edit, or cancel. Edited drafts are checked again.",
                   patterns=[r"human: draft review"], function="_review"),
    "finish": node("Ready for Export", "control", 490, 2690, "Persist the reviewed draft. The export API renders DOCX/PDF/ZIP from this saved state.",
                   patterns=[r"complete: reviewed", r"complete: draft ready"], function="_finish"),
    "cancel": node("Cancel Draft", "control", 990, 2690, "Retain the cancelled case in the local checkpoint database.",
                   patterns=[r"complete: cancelled"], function="_cancel"),
    "__end__": node("End", "control", 740, 2860, "Graph execution ends; the saved case can still be inspected/exported."),
}


OVERVIEW = [
    ("input", "Requirement Input", "control", 40, 40, ["__start__", "intake"]),
    ("analyst", "Requirement Analyst", "agent", 390, 40, ["extract", "retrieve", "review_evidence", "assemble"]),
    ("clarify", "Human Clarification", "human", 740, 40, ["clarification"]),
    ("supervisor", "Supervisor Agent", "agent", 1090, 40, ["supervisor"]),
    ("solution", "Solution Architect", "agent", 930, 240, ["solution"]),
    ("delivery", "Delivery Estimator", "agent", 1280, 240, ["delivery"]),
    ("commercial", "Commercial Analyst", "agent", 930, 420, ["commercial"]),
    ("risk", "Risk & Dependency Agent", "agent", 1280, 420, ["risk"]),
    ("join", "Reconcile Findings", "tool", 580, 330, ["reconcile", "prepare"]),
    ("proposal", "Proposal Writer", "agent", 230, 240, ["write_proposal", "enrich_proposal"]),
    ("brd", "BRD Writer", "agent", 230, 420, ["write_brd"]),
    ("validation", "Validation Tool", "tool", 40, 680, ["validate"]),
    ("quality", "Quality Critic", "agent", 390, 680, ["critic", "critic_packet"]),
    ("revision", "Revision · One Attempt", "agent", 390, 880, ["repair"]),
    ("reject", "Reject", "control", 740, 880, ["reject"]),
    ("human", "Human Draft Review", "human", 740, 680, ["review"]),
    ("export", "Reviewed → Export", "control", 1090, 680, ["finish"]),
    ("cancel", "Cancel", "control", 1090, 880, ["cancel"]),
]

EDGE_LABELS = {
    ("intake", "extract"): "new case",
    ("intake", "supervisor"): "pre-analyzed mode",
    ("validate", "critic"): "passes",
    ("validate", "repair"): "scope repair",
    ("validate", "reject"): "cannot repair",
    ("critic", "critic_packet"): "new / changed draft",
    ("critic_packet", "critic_packet"): "next packet",
    ("review", "validate"): "accept / edited draft",
    ("review", "cancel"): "cancel",
    ("repair", "validate"): "re-check",
}
for critic_node in ("critic", "critic_packet"):
    EDGE_LABELS.update({
        (critic_node, "repair"): "one repair",
        (critic_node, "reject"): "blocking issues",
        (critic_node, "review"): "passes → human review",
        (critic_node, "finish"): "accepted / direct mode",
    })
for specialist in ("solution", "delivery", "commercial", "risk"):
    EDGE_LABELS[("supervisor", specialist)] = "parallel"
    EDGE_LABELS[(specialist, "reconcile")] = "wait for all four"


def _status(identifier, metadata, snapshot):
    if snapshot is None:
        return "idle"
    for task in snapshot.tasks:
        if task.name == identifier:
            if task.interrupts:
                return "waiting"
            if task.error:
                return "failed"
    if identifier in snapshot.next:
        return "pending"
    if identifier == "__start__" and snapshot.values:
        return "completed"
    if identifier == "__end__" and not snapshot.next:
        return "completed"
    if any(re.search(pattern, step) for pattern in metadata["trace_patterns"]
           for step in snapshot.values.get("trace", [])):
        return "completed"
    return "idle"


def describe_workflow(snapshot=None):
    """Keep exact-view edges sourced from the real compiled graph, not a sketch."""
    compiled = multi_agent_graph._STATELESS_GRAPH.get_graph()
    actual = []
    for identifier in compiled.nodes:
        metadata = NODES[identifier]
        actual.append({"id": identifier, **metadata, "status": _status(identifier, metadata, snapshot)})
    by_id = {item["id"]: item for item in actual}
    edges = [{"source": edge.source, "target": edge.target,
              "conditional": edge.conditional,
              "label": EDGE_LABELS.get((edge.source, edge.target), str(edge.data or ""))}
             for edge in compiled.edges]
    overview = []
    groups = {}
    for identifier, label, kind, x, y, members in OVERVIEW:
        groups.update({member: identifier for member in members})
        states = [by_id[member]["status"] for member in members]
        status = next((state for state in ("failed", "waiting", "pending", "completed") if state in states), "idle")
        overview.append({"id": identifier, "label": label, "kind": kind, "x": x, "y": y,
                         "members": members, "status": status})
    # Fold internal tools/steps into role cards; retain the compiled inter-group routes.
    overview_edges = {}
    for edge in edges:
        if edge["target"] == "__end__":
            continue
        source, target = groups[edge["source"]], groups[edge["target"]]
        if source == target:
            continue
        overview_edges[(source, target)] = {**edge, "source": source, "target": target}
    roles = {item["role"] for item in actual if item["role"]}
    return {
        "summary": {"main_roles_per_run": len(roles - {"proposal_writer", "brd_writer", "revision_writer"}) + 1,
                    "optional_revision_roles": 1, "unique_role_implementations": len(roles),
                    "parallel_specialists": 4, "graph_steps": len(actual) - 2,
                    "graph_nodes": len(actual), "graph_edges": len(edges)},
        "nodes": actual, "edges": edges, "overview_nodes": overview,
        "overview_edges": list(overview_edges.values()),
        "case": None if snapshot is None else {
            "status": "waiting for human input" if any(task.interrupts for task in snapshot.tasks)
                      else snapshot.values.get("status"),
            "document_type": snapshot.values.get("document_type"),
            "pending_steps": list(snapshot.next), "trace": snapshot.values.get("trace", []),
        },
    }
