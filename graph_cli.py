#!/usr/bin/env python3
"""Run the persistent LangGraph case workflow from a terminal.

This is the reference human-in-the-loop path. It pauses first for
clarification answers and then before document release. The SQLite
checkpointer lets the run resume with the same --thread-id.
"""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from langgraph.types import Command

import business_requirements_agent as brd_agent
import graph_runtime
import project_records
import wooplix_agent as brand


def _question_answers(payload):
    answers = {}
    print("\nClarification review")
    print(payload.get("summary", ""))
    for question in payload.get("questions", []):
        print(f"\n{question['id']}: {question['question']}")
        if question.get("reason"):
            print(f"Why: {question['reason']}")
        for index, option in enumerate(question.get("options", []), 1):
            print(f"  {index}. {option}")
        answer = input("Answer (blank = Leave open for discovery): ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(question.get("options", [])):
            answer = question["options"][int(answer) - 1]
        answers[question["id"]] = answer or "Leave open for discovery"
    return {"answers": answers}


def _review_response(payload):
    print("\nDraft review required before export.")
    print(f"Document type: {payload.get('document_type')}")
    print(json.dumps(payload["document"], indent=2, ensure_ascii=False))
    decision = input("Type accept or cancel: ").strip().casefold()
    if decision not in {"accept", "cancel"}:
        decision = "cancel"
    return {
        "decision": decision,
        "reviewer": input("Reviewer name: ").strip() or "CLI reviewer",
        "edited_document": None,
    }


def _run(graph, initial, config):
    result = graph.invoke(initial, config=config)
    while "__interrupt__" in result:
        interrupt_value = result["__interrupt__"][0].value
        if interrupt_value.get("stage") == "clarification":
            response = _question_answers(interrupt_value)
        elif interrupt_value.get("stage") == "draft_review":
            response = _review_response(interrupt_value)
        else:
            raise RuntimeError("Unknown LangGraph interrupt stage.")
        result = graph.invoke(Command(resume=response), config=config)
    return result


def main():
    parser = argparse.ArgumentParser(description="Wooplix persistent LangGraph workflow")
    parser.add_argument("requirement", help="TXT, MD, DOCX or PDF requirement")
    parser.add_argument("--document-type", choices=("proposal", "brd"), default="proposal")
    parser.add_argument("--state-db", default=".state/wooplix.sqlite")
    parser.add_argument("--thread-id", default=None)
    parser.add_argument("--out", default="out/langgraph")
    args = parser.parse_args()

    path = Path(args.requirement).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"Requirement not found: {path}")
    if not brand.GROQ_API_KEY:
        raise SystemExit("GROQ_API_KEY is not set. Put it in .env or export it.")

    records, notices = project_records.load_project_records()
    requirement = brand.extract_requirement(str(path))
    db_path = Path(args.state_db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    graph_runtime.DB_PATH = db_path
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    thread_id = args.thread_id or str(uuid.uuid4())

    graph = graph_runtime.get_graph()
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
    snapshot = graph.get_state(config)
    if snapshot.values:
        if snapshot.values.get("document_type") != args.document_type or snapshot.values.get("requirement") != requirement:
            raise SystemExit("This thread belongs to a different requirement. Choose a new --thread-id.")
        initial = None
    else:
        initial = {
        "mode": "case",
        "document_type": args.document_type,
        "requirement": requirement,
        "source": path.name,
        "completed": records,
        "project_data": records,
        "crm": "",
        "trace": [],
        "repair_count": 0,
        }
    result = _run(graph, initial, config)

    if result.get("status") == "cancelled":
        print("Draft cancelled. The checkpoint is retained for audit/review.")
        return
    if result.get("status") != "reviewed":
        raise SystemExit(f"The run did not reach reviewed status: {result.get('status')}")

    if args.document_type == "proposal":
        document = result["proposal"]
        stem = "Wooplix_Proposal_" + (document.get("client", {}).get("company_name") or path.stem)
        stem = "".join(char if char.isalnum() or char in "-_" else "_" for char in stem)
        docx_path = output / f"{stem}.docx"
        json_path = output / f"{stem}.json"
        brand.build_docx(document, str(docx_path))
        json_path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Reviewed proposal DOCX: {docx_path}")
        print(f"Reviewed proposal JSON: {json_path}")
    else:
        document = result["document"]
        stem = "Wooplix_Business_Requirements_" + path.stem
        stem = "".join(char if char.isalnum() or char in "-_" else "_" for char in stem)
        docx_path = output / f"{stem}.docx"
        json_path = output / f"{stem}.json"
        brd_agent.build_docx(document, str(docx_path))
        json_path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Reviewed BRD DOCX: {docx_path}")
        print(f"Reviewed BRD JSON: {json_path}")
    print(f"Thread ID: {thread_id}")
    if notices:
        print("Notices:", notices)


if __name__ == "__main__":
    main()
