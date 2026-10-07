"""Deterministic evidence, reconciliation, rendering and repair tools."""
import copy
import hashlib
import re

from multi_agent_contracts import Requirement
from proposal_scope import product_matches


QUALIFIED_SCOPE = re.compile(
    r"\b(?:not required (?:in|for|during) (?:the |this |current )?phase|out of scope|"
    r"excluded from (?:this |the |current )?scope|future phase|future scope|"
    r"future enhancement|optional feature)\b", re.I
)


def requirement_register(context):
    """Assign content-stable IDs; never confuse a source statement with approval."""
    rows, seen = [], set()
    for section in context["analysis"].get("requirement_sections", []):
        for value in section.get("requirements", []):
            text = re.sub(r"\s+", " ", value).strip()
            key = (section["product"].casefold(), text.casefold())
            if not text or key in seen:
                continue
            seen.add(key)
            identifier = "REQ-" + hashlib.sha256("\0".join(key).encode()).hexdigest()[:10].upper()
            rows.append(Requirement(
                id=identifier, area=section["product"], requirement=text,
                source=context.get("source", "Pasted requirement"), source_excerpt=text,
                inclusion="qualified" if QUALIFIED_SCOPE.search(text) else "requested",
            ).model_dump())
    return rows


def specialist_payload(context, task=None):
    register = context.get("requirement_register") or requirement_register(context)
    records = context.get("completed", []) + context.get("timing_sources", [])
    by_id = {row["record_id"]: row for row in records if row.get("record_id")}
    return {
        "requirements": register,
        "reviewed_answers": [
            {"question": row["question"], "answer": context.get("answers", {}).get(row["id"], "Leave open for discovery")}
            for row in context["analysis"].get("questions", [])
        ],
        "delivery_evidence": [{k: row.get(k) for k in ("record_id", "product", "scope", "days", "kind")}
                              for row in by_id.values()],
        "task": task or {},
    }


def check_references(report, context):
    """Reject invented IDs and unrequested products at the specialist boundary."""
    ids = {row["id"] for row in context["requirement_register"]}
    records = context.get("completed", []) + context.get("timing_sources", [])
    evidence_ids = {row["record_id"] for row in records if row.get("record_id")}
    products = [section["product"] for section in context["analysis"]["requirement_sections"]]
    source_text = context["requirement"] + " " + " ".join(context.get("answers", {}).values())
    source_numbers = set(re.findall(r"\b\d+(?:[.,]\d+)*\b", source_text))

    def visit(value):
        if isinstance(value, dict):
            if "requirement_ids" in value:
                if not value["requirement_ids"] or not set(value["requirement_ids"]).issubset(ids):
                    raise ValueError("Specialist findings must reference supplied requirement IDs.")
            if "evidence_ids" in value and not set(value["evidence_ids"]).issubset(evidence_ids):
                raise ValueError("Specialist findings reference unknown evidence.")
            if "product" in value and not any(product_matches(value["product"], name) or
                                              product_matches(name, value["product"]) for name in products):
                raise ValueError("A specialist proposed an unrequested product.")
            for key, child in value.items():
                if key not in {"requirement_ids", "evidence_ids"}:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            if not set(re.findall(r"\b\d+(?:[.,]\d+)*\b", value)).issubset(source_numbers):
                raise ValueError("Specialist prose contains an unsupported numeric claim.")
    visit(report)
    return report


def output_path(document, path):
    """Resolve only dotted/list text paths, never evaluate model-supplied code."""
    if not re.fullmatch(r"[a-z_]+(?:\[\d+\]|\.[a-z_]+)*", path):
        raise ValueError("Invalid document path.")
    parts = re.findall(r"([a-z_]+)|\[(\d+)\]", path)
    keys = [name if name else int(index) for name, index in parts]
    value = document
    for key in keys:
        value = value[key]
    return keys, value


def critic_batches(document, max_chars=7000):
    """Review generated prose in bounded packets, preserving original paths.

    A long BRD should not exceed a provider's per-request token allowance.
    Source tables/registers and audit copies need deterministic coverage checks,
    not repeated language-model review. Each batch is separately checkpointed.
    """
    fields = []
    excluded = {"agent_audit", "agent_trace", "requirement_traceability", "requirements", "areas",
                "business_rules", "data_requirements", "integration_requirements", "reporting_requirements",
                "access_requirements", "requirements_by_area", "activity_map"}

    def visit(value, path):
        if isinstance(value, str) and value.strip():
            for offset in range(0, len(value), 3000):
                fields.append({"path": path, "text": value[offset:offset + 3000]})
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in {"requirement_ids", "evidence_ids", "id"}:
                    continue
                visit(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    for key, value in document.items():
        if key in excluded:
            continue
        if key == "specification_tables":
            for index, table in enumerate(value):
                if table.get("origin") != "source":
                    visit(table, f"specification_tables[{index}]")
        else:
            visit(value, key)
    packets, packet, length = [], [], 0
    for field in fields:
        size = len(field["path"]) + len(field["text"]) + 32
        if packet and length + size > max_chars:
            packets.append(packet); packet, length = [], 0
        packet.append(field); length += size
    if packet:
        packets.append(packet)
    return packets or [[]]


def apply_text_patches(document, patches):
    """Edits are limited to generated prose; source scope and numbers are locked."""
    result = copy.deepcopy(document)
    for patch in patches:
        path = patch["path"]
        allowed = (re.fullmatch(r"(?:project_introduction|summary|prerequisites\[\d+\]|deliverables\[\d+\]|open_points\[\d+\])", path)
                   or re.fullmatch(r"specification_tables\[\d+\]\.rows\[\d+\]\[\d+\]", path)
                   or re.fullmatch(r"solution_recommendations\[\d+\]\.(?:design|reason)", path)
                   or re.fullmatch(r"risks_and_dependencies\[\d+\]\.(?:description|impact|mitigation)", path))
        if not allowed:
            raise ValueError("Critic repairs can only edit generated prose.")
        keys, current = output_path(result, path)
        if not isinstance(current, str):
            raise ValueError("Critic repair path is not a text field.")
        if path.startswith("specification_tables"):
            table = result["specification_tables"][keys[1]]
            if table.get("origin") != "proposed":
                raise ValueError("Source tables cannot be edited by the critic.")
        parent = result
        for key in keys[:-1]:
            parent = parent[key]
        parent[keys[-1]] = patch["replacement"]
    return result


def attach_findings(document, findings, register, quality=None):
    """Add client-visible planning details plus an internal traceable audit."""
    result = copy.deepcopy(document)
    ids = {row["id"]: row for row in register}
    solution = findings["solution"]
    risk = findings["risk"]
    decision_rows = solution.get("open_decisions", []) + findings["delivery"].get("open_decisions", [])
    decision_rows += findings["commercial"].get("dependencies", [])
    result["solution_recommendations"] = [
        {"product": row["product"], "design": row["design"], "reason": row["reason"],
         "status": row["status"], "requirement_ids": row["requirement_ids"]}
        for row in solution.get("solutions", [])
    ]
    result["delivery_dependencies"] = findings["delivery"].get("dependencies", [])
    result["risks_and_dependencies"] = risk.get("risks", [])
    result["planning_decisions"] = decision_rows
    result["requirement_traceability"] = [
        {"id": row["id"], "area": row["area"], "source": row["source"],
         "requirement": row["requirement"], "inclusion": row["inclusion"]}
        for row in register
    ]
    result["agent_audit"] = {"specialist_findings": findings, "requirement_register": list(ids.values()),
                             "quality_report": quality or {}}
    return result


def planning_tables(document):
    """One set of section/table definitions feeds all four export renderers."""
    tables = []
    solutions = document.get("solution_recommendations", [])
    if solutions:
        tables.append(("Solution Recommendations — Proposed for Review",
                       ["Product", "Proposed design", "Rationale", "Status"],
                       [[row["product"], row["design"], row["reason"], row["status"]] for row in solutions]))
    dependencies = document.get("delivery_dependencies", [])
    if dependencies:
        tables.append(("Delivery Dependencies — Proposed for Review", ["Phase", "Dependency", "Condition"],
                       [[row["phase"], row["description"], row["condition"]] for row in dependencies]))
    risks = document.get("risks_and_dependencies", [])
    if risks:
        tables.append(("Risks and Planning Assumptions", ["Risk", "Impact", "Mitigation", "Status"],
                       [[row["description"], row["impact"], row["mitigation"], row["status"]] for row in risks]))
    decisions = document.get("planning_decisions", [])
    if decisions:
        tables.append(("Open Decisions", ["Decision to confirm", "Why it matters"],
                       [[row["question"], row["reason"]] for row in decisions]))
    register = document.get("requirement_traceability", [])
    if register:
        tables.append(("Requirement Traceability", ["ID", "Business area", "Source", "Source requirement"],
                       [[row["id"], row["area"], row["source"], row["requirement"]] for row in register]))
    return tables
