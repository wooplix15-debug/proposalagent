"""Vercel API for turning one or more requirement files into a ZIP result folder."""
import io
import json
import os
import re
import requests
import tempfile
import zipfile
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from typing import Optional, List
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from langgraph.types import Command
from pydantic import BaseModel
import sys
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wooplix_agent as agent
import proposal_workflow as workflow
import project_records
import business_requirements_agent as brd_agent
import agent_graph
import graph_runtime
import multi_agent_graph
import workflow_view
import telegram_bot
from graph_schemas import Analysis, validate_answers

@asynccontextmanager
async def lifespan(application):
    application.state.telegram = telegram_bot.start_configured_bot()
    try:
        yield
    finally:
        if application.state.telegram:
            await run_in_threadpool(application.state.telegram.stop)


app = FastAPI(title="Wooplix Proposal Agent — LangGraph", lifespan=lifespan)


def _graph_result(result, thread_id):
    """Convert a LangGraph result or interrupt into a frontend-safe response."""
    interrupts = result.get("__interrupt__", [])
    if interrupts:
        value = interrupts[0].value
        return {
            "status": "interrupted",
            "thread_id": thread_id,
            "document_type": result.get("document_type"),
            "stage": value.get("stage", "review"),
            "payload": value,
            "trace": result.get("trace", []),
        }
    response = {
        "status": result.get("status", "complete"),
        "thread_id": thread_id,
        "document_type": result.get("document_type"),
        "trace": result.get("trace", []),
        "quality_report": result.get("quality_report", {}),
        "specialist_findings": result.get("specialist_findings", {}),
    }
    if "proposal" in result:
        response["proposal"] = result["proposal"]
    if "document" in result:
        response["document"] = result["document"]
    return response


class AgentResumeRequest(BaseModel):
    thread_id: str
    response: dict


class AgentThreadRequest(BaseModel):
    thread_id: str
MAX_FILES = 5
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_BATCH_BYTES = 15 * 1024 * 1024
ALLOWED_SUFFIXES = {".txt", ".md", ".doc", ".docx", ".pdf"}


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "Client"


def _build_pdf_with_existing_template(proposal, target, host=None):
    """Render the exact same HTML through the same Dompdf options as the CLI."""
    if os.environ.get("VERCEL"):
        base = (os.environ.get("PDF_RENDER_BASE_URL") or
                os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL"))
        token = os.environ.get("PDF_RENDER_TOKEN")
        if not base or not token:
            raise RuntimeError("Vercel PHP/Dompdf renderer is not configured")
        url = base if base.startswith("http") else f"https://{base}"
        url = f"{url.rstrip('/')}/api/pdf.php"
        response = requests.post(
            url,
            json={"html": agent.build_html(proposal)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=240,
        )
        response.raise_for_status()
        Path(target).write_bytes(response.content)
        return target
    return agent.build_pdf(proposal, str(target))


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
def index_page():
    for candidate in [
        Path(__file__).resolve().parents[1] / "index.html",
        Path(__file__).resolve().parents[1] / "public" / "index.html",
    ]:
        if candidate.exists():
            return HTMLResponse(content=candidate.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Wooplix Proposal Agent</h1>")


@app.get("/workflow", response_class=HTMLResponse)
def workflow_page():
    page = Path(__file__).resolve().parents[1] / "public" / "workflow.html"
    return HTMLResponse(content=page.read_text(encoding="utf-8"))


@app.get("/api/agent/workflow")
def visual_workflow(thread_id: Optional[str] = None):
    """Read graph topology and optional saved progress without running any agents."""
    snapshot = None
    if thread_id is not None:
        if not thread_id.strip():
            raise HTTPException(400, "Supply a thread_id or omit it to view the architecture.")
        snapshot = graph_runtime.get_graph().get_state({"configurable": {"thread_id": thread_id}})
        if not snapshot.values:
            raise HTTPException(404, "Agent thread not found.")
    result = workflow_view.describe_workflow(snapshot)
    if result["case"] is not None:
        result["case"]["thread_id"] = thread_id
    return result


@app.get("/wooplix_logo.png")
def logo():
    for candidate in [
        Path(__file__).resolve().parents[1] / "public" / "wooplix_logo.png",
        Path(__file__).resolve().parents[1] / "wooplix_logo.png",
    ]:
        if candidate.exists():
            return FileResponse(str(candidate), media_type="image/png")
    raise HTTPException(status_code=404, detail="Logo not found")


@app.get("/api/health")
@app.get("/api/index.py/health")
@app.get("/health")
@app.get("/api/index.py")
def health():
    bot = getattr(app.state, "telegram", None)
    return {"ok": True, "configured": bool(agent.GROQ_API_KEY), "max_files": MAX_FILES,
            "orchestrator": "langgraph-multi-agent",
            "specialists": ["solution_architect", "delivery_estimator",
                            "commercial_analyst", "risk_and_dependency"],
            "persistent_reviews": "graph_cli.py and /api/agent/* (SQLite)",
            "telegram": {"configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
                         "status": bot.status if bot else "disabled", "username": bot.username if bot else None}}


@app.post("/api/agent/start")
@app.post("/agent/start")
async def start_persistent_agent(text: Optional[str] = Form(None),
                                 document_type: str = Form("proposal"),
                                 thread_id: Optional[str] = Form(None),
                                 files: Optional[List[UploadFile]] = File(None)):
    """Start a persistent local LangGraph case without authentication."""
    if not agent.GROQ_API_KEY:
        raise HTTPException(503, "Set GROQ_API_KEY before starting the agent.")
    if document_type not in {"proposal", "brd"}:
        raise HTTPException(400, "Choose proposal or brd.")
    source = "Pasted requirement"
    if files:
        if len(files) != 1 or (text and text.strip()):
            raise HTTPException(400, "Supply one requirement file or pasted text per case.")
        with tempfile.TemporaryDirectory(prefix="wooplix-case-") as work:
            rows = await _extract_uploads(files, work, "case")
        if rows:
            text, source = rows[0]["text"], rows[0]["source"]
    if not text or not text.strip():
        raise HTTPException(400, "Supply a requirement.")
    if len(text) > 60000:
        raise HTTPException(413, "Shorten the requirement to 60,000 characters.")
    records, _ = project_records.load_project_records()
    thread = thread_id or str(uuid4())
    config = {"configurable": {"thread_id": thread}, "recursion_limit": 60}
    graph = graph_runtime.get_graph()
    snapshot = await run_in_threadpool(graph.get_state, config)
    if snapshot.values:
        raise HTTPException(409, "This thread already exists. Resume it or use a new thread ID.")
    try:
        result = await run_in_threadpool(
            graph.invoke,
            {
                "mode": "case", "document_type": document_type,
                "requirement": text.strip(), "source": source,
                "completed": records, "project_data": records, "crm": "",
                "trace": [], "repair_count": 0,
            },
            config,
        )
    except workflow.AnalysisServiceError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, "The agent could not validate the requirement or specialist response.") from exc
    except Exception as exc:
        print(f"Persistent agent start failed: {type(exc).__name__}")
        raise HTTPException(502, "The persistent agent could not start.") from exc
    return _graph_result(result, thread)


@app.post("/api/agent/resume")
@app.post("/agent/resume")
async def resume_persistent_agent(request: AgentResumeRequest):
    """Resume a clarification or draft-review interrupt using the thread ID."""
    if not request.thread_id.strip():
        raise HTTPException(400, "Supply a thread_id.")
    config = {"configurable": {"thread_id": request.thread_id}, "recursion_limit": 60}
    graph = graph_runtime.get_graph()
    snapshot = await run_in_threadpool(graph.get_state, config)
    if not snapshot.values:
        raise HTTPException(404, "Agent thread not found.")
    pending = [item for task in snapshot.tasks for item in task.interrupts]
    if not pending:
        raise HTTPException(409, "This thread is not waiting for human input.")
    try:
        result = await run_in_threadpool(
            graph.invoke,
            Command(resume=request.response),
            config,
        )
    except workflow.AnalysisServiceError as exc:
        raise HTTPException(503, str(exc)) from exc
    except agent_graph.AgentGraphError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        print(f"Persistent agent resume failed: {type(exc).__name__}")
        raise HTTPException(400, "The agent could not resume this thread.") from exc
    return _graph_result(result, request.thread_id)


@app.post("/api/agent/retry")
async def retry_persistent_agent(request: AgentThreadRequest):
    """Continue failed nodes from their checkpoint, keeping successful work."""
    graph = graph_runtime.get_graph()
    config = {"configurable": {"thread_id": request.thread_id}, "recursion_limit": 60}
    snapshot = await run_in_threadpool(graph.get_state, config)
    if not snapshot.values:
        raise HTTPException(404, "Agent thread not found.")
    if not snapshot.next or any(task.interrupts for task in snapshot.tasks):
        raise HTTPException(409, "This thread has no failed steps to retry.")
    try:
        result = await run_in_threadpool(graph.invoke, None, config)
    except workflow.AnalysisServiceError as exc:
        raise HTTPException(503, str(exc)) from exc
    except agent_graph.AgentGraphError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "The saved agent steps could not complete.") from exc
    return _graph_result(result, request.thread_id)


@app.get("/api/agent/state/{thread_id}")
def persistent_agent_state(thread_id: str):
    """Read the saved draft and specialist trace for local review."""
    snapshot = graph_runtime.get_graph().get_state({"configurable": {"thread_id": thread_id}})
    if not snapshot.values:
        raise HTTPException(404, "Agent thread not found.")
    result = dict(snapshot.values)
    pending = [item for task in snapshot.tasks for item in task.interrupts]
    if pending:
        result["__interrupt__"] = pending
    response = _graph_result(result, thread_id)
    response["retryable"] = bool(snapshot.next and not pending)
    response["pending_steps"] = list(snapshot.next)
    return response


def _brd_pdf(document, target):
    if not os.environ.get("VERCEL"):
        return brd_agent.build_pdf(document, str(target))
    base = (os.environ.get("PDF_RENDER_BASE_URL") or
            os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL"))
    token = os.environ.get("PDF_RENDER_TOKEN")
    if not base or not token:
        raise RuntimeError("PDF renderer is not configured.")
    url = base if base.startswith("http") else f"https://{base}"
    response = requests.post(f"{url.rstrip('/')}/api/pdf.php", json={"html": brd_agent.build_html(document)},
                             headers={"Authorization": f"Bearer {token}"}, timeout=240)
    response.raise_for_status()
    Path(target).write_bytes(response.content)


@app.get("/api/agent/export/{thread_id}")
async def export_persistent_agent(thread_id: str, format: str = "docx"):
    """Export the exact reviewed checkpoint; no re-generation/model calls."""
    if format not in {"docx", "pdf", "zip"}:
        raise HTTPException(400, "Choose PDF, Word or ZIP format.")
    snapshot = await run_in_threadpool(graph_runtime.get_graph().get_state,
                                       {"configurable": {"thread_id": thread_id}})
    state = snapshot.values
    if not state:
        raise HTTPException(404, "Agent thread not found.")
    if state.get("status") != "reviewed" or snapshot.next:
        raise HTTPException(409, "Review the current draft before exporting.")
    document_type = state["document_type"]
    document = state["proposal"] if document_type == "proposal" else state["document"]
    name = ((document.get("client") or {}).get("company_name") if document_type == "proposal"
            else document.get("project_name")) or "Project"
    stem = f"Wooplix_{'Proposal' if document_type == 'proposal' else 'Business_Requirements'}_{_safe_name(name)}"
    try:
        with tempfile.TemporaryDirectory(prefix="wooplix-reviewed-") as work:
            outputs = []
            if format in {"docx", "zip"}:
                target = Path(work) / f"{stem}.docx"
                renderer = agent.build_docx if document_type == "proposal" else brd_agent.build_docx
                await run_in_threadpool(renderer, document, str(target))
                outputs.append(target)
            if format in {"pdf", "zip"}:
                target = Path(work) / f"{stem}.pdf"
                renderer = _build_pdf_with_existing_template if document_type == "proposal" else _brd_pdf
                await run_in_threadpool(renderer, document, target)
                outputs.append(target)
            if format == "zip":
                audit = Path(work) / f"{stem}.json"
                audit.write_text(json.dumps(dict(document, agent_trace=state.get("trace", [])),
                                           indent=2, ensure_ascii=False), encoding="utf-8")
                outputs.append(audit)
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                    for path in outputs:
                        bundle.write(path, arcname=path.name)
                content = archive.getvalue()
            else:
                content = outputs[0].read_bytes()
    except Exception as exc:
        raise HTTPException(502, "The reviewed document could not be exported.") from exc
    media_type = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                  "pdf": "application/pdf", "zip": "application/zip"}[format]
    filename = f"{stem}.{format}"
    return StreamingResponse(io.BytesIO(content), media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Proposal-Filename": filename, "X-Proposal-Client": _safe_name(name),
    })


async def _extract_uploads(files, work, prefix):
    rows = []
    total = 0
    for index, upload in enumerate(files or [], 1):
        if not upload.filename:
            continue
        name = Path(upload.filename).name
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(400, f"{name}: use TXT, MD, DOC, DOCX, or PDF.")
        raw = await upload.read(MAX_FILE_BYTES + 1)
        total += len(raw)
        if len(raw) > MAX_FILE_BYTES or total > MAX_BATCH_BYTES:
            raise HTTPException(413, "Each file must be under 4 MB and each group under 15 MB.")
        path = Path(work) / f"{prefix}-{index}{suffix}"
        path.write_bytes(raw)
        try:
            content = agent.extract_requirement(str(path))
        except (Exception, SystemExit) as exc:
            raise HTTPException(400, f"Could not read {name}. Try DOCX or a text-based PDF.") from exc
        if not content.strip():
            raise HTTPException(400, f"{name}: no readable text found. Scanned PDFs need text extraction first.")
        rows.append({'source': name, 'text': content})
    return rows


@app.post("/api/analyze")
@app.post("/api/index.py/analyze")
@app.post("/analyze")
async def analyze_requirement(files: Optional[List[UploadFile]] = File(None),
                              text: Optional[str] = Form(None),
                              completed_files: Optional[List[UploadFile]] = File(None),
                              completed_text: Optional[str] = Form(None)):
    if not agent.GROQ_API_KEY:
        raise HTTPException(503, "Set GROQ_API_KEY before analyzing requirements.")
    if len(files or []) > MAX_FILES or len(completed_files or []) > MAX_FILES:
        raise HTTPException(400, "Upload at most five requirements and five completed project records.")
    with tempfile.TemporaryDirectory(prefix='wooplix-analysis-') as work:
        requirements = await _extract_uploads(files, work, 'requirement')
        completed = [dict(row, kind='completed_project')
                     for row in await _extract_uploads(completed_files, work, 'completed')]
    if text and text.strip():
        requirements.append({'source': 'Pasted requirement', 'text': text.strip()})
    if completed_text and completed_text.strip():
        completed.append({'source': 'Pasted completed project record', 'text': completed_text.strip(),
                          'kind': 'completed_project'})
    if not requirements or len(requirements) > MAX_FILES:
        raise HTTPException(400, "Supply between one and five requirements.")
    # Explicit limit: do not silently drop evidence from the comparison.
    if sum(len(x['text']) for x in requirements + completed) > 60000:
        raise HTTPException(413, "Please shorten the requirement and delivery records to 60,000 characters in total.")
    project_data, notices = project_records.load_project_records()
    completed = project_data + completed
    if sum(len(x['text']) for x in requirements + completed) > 60000:
        raise HTTPException(413, 'The completed project library is too large. Use fewer or shorter records.')
    crm = ''
    reviews = []
    try:
        for row in requirements:
            analysis, trace = await run_in_threadpool(
                agent_graph.run_analysis, row['text'], completed, crm, project_data)
            context_record_ids = set(analysis.get('context_record_ids', []))
            context_record_ids.update(x['record_id'] for x in analysis.get('duration_estimates', [])
                                      if x.get('record_id'))
            context_sources = set(analysis.get('context_sources', []))
            relevant_completed = [x for x in completed
                                  if x.get('record_id') in context_record_ids
                                  or (x.get('source') in context_sources and not x.get('record_id'))]
            relevant_timing = [x for x in project_data if x.get('record_id') in context_record_ids]
            context = {'requirement': row['text'], 'source': row['source'],
                       'completed': relevant_completed, 'timing_sources': relevant_timing,
                       'crm': crm, 'analysis': analysis}
            reviews.append(dict(analysis, source=row['source'], review_token=workflow.seal(context),
                                agent_trace=trace))
    except workflow.AnalysisServiceError as exc:
        print(f'Analysis service unavailable: {type(exc).__name__}')
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        print(f'Analysis input failed: {type(exc).__name__}')
        raise HTTPException(400, f"We could not read the requirement structure: {exc}") from exc
    except Exception as exc:
        print(f'Analysis failed: {type(exc).__name__}')
        raise HTTPException(502, 'The requirements could not be analyzed. Please try again.') from exc
    return {'reviews': reviews, 'notices': notices,
            'evidence_note': ''}


@app.post("/api/generate")
@app.post("/api/index.py/generate")
@app.post("/generate")
@app.post("/api/index.py")
async def generate(request: Request, reviews: str = Form(...)):
    if not agent.GROQ_API_KEY:
        raise HTTPException(503, "Set GROQ_API_KEY before generating proposals.")
    try:
        reviewed = json.loads(reviews)
        if not isinstance(reviewed, list) or not 1 <= len(reviewed) <= MAX_FILES:
            raise ValueError('Supply one to five analyzed requirements.')
        prepared = []
        for row in reviewed:
            context = workflow.unseal(row['review_token'])
            Analysis.model_validate(context['analysis'])
            answers = validate_answers(context['analysis'], row.get('answers', {}))
            prepared.append((context, answers))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except (RuntimeError, workflow.AnalysisServiceError) as exc:
        raise HTTPException(503, str(exc)) from exc

    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    requested_format = (request.query_params.get("format") or "auto").lower()
    if requested_format not in {"pdf", "docx", "zip", "auto"}:
        raise HTTPException(400, "Choose PDF, Word or ZIP format.")
    archive = io.BytesIO()
    total = 0
    names = set()
    single_pdf_bytes = None
    single_docx_bytes = None
    single_client = ""
    single_stem = ""

    try:
        with tempfile.TemporaryDirectory(prefix="wooplix-") as work:
            item_count = len(prepared)
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for index, (context, answers) in enumerate(prepared, start=1):
                    source_name = context['source']
                    proposal, trace = await run_in_threadpool(multi_agent_graph.run_proposal, context, answers)
                    client_name = (proposal.get("client") or {}).get("company_name") or Path(source_name).stem
                    stem = _safe_name(client_name)
                    if stem in names:
                        stem = f"{stem}_{index}"
                    names.add(stem)
                    result_dir = Path(work) / f"result-{index}"
                    result_dir.mkdir()
                    docx = result_dir / f"Wooplix_Proposal_{stem}.docx"
                    pdf = result_dir / f"Wooplix_Proposal_{stem}.pdf"
                    json_path = result_dir / f"Wooplix_Proposal_{stem}.json"
                    agent.build_docx(proposal, str(docx))
                    json_path.write_text(json.dumps(dict(proposal, agent_trace=trace), indent=2,
                                                    ensure_ascii=False), encoding="utf-8")
                    outputs = [docx, json_path]
                    try:
                        if requested_format != 'docx' or item_count > 1:
                            await run_in_threadpool(_build_pdf_with_existing_template, proposal, pdf, host)
                        if pdf.exists() and pdf.stat().st_size > 0:
                            outputs.append(pdf)
                    except Exception as pdf_err:
                        print(f"PDF build warning: {type(pdf_err).__name__}: {pdf_err}")
                    for path in outputs:
                        bundle.write(path, arcname=f"Wooplix_Results/{path.name}")

                    if item_count == 1:
                        single_client = client_name
                        single_stem = stem
                        if docx.exists():
                            single_docx_bytes = docx.read_bytes()
                        if pdf.exists() and pdf.stat().st_size > 0:
                            single_pdf_bytes = pdf.read_bytes()

            archive.seek(0)
            zip_payload = archive.read()
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Proposal generation failed: {type(exc).__name__}: {str(exc)[:300]}")
        raise HTTPException(status_code=502, detail="Proposal generation failed. Please try again.") from exc

    if item_count == 1 and requested_format == 'pdf' and not single_pdf_bytes:
        raise HTTPException(502, 'Could not render the PDF. Please try again or select Word.')

    # Direct PDF response when generating a single proposal (default format)
    if item_count == 1 and requested_format in ("pdf", "auto") and single_pdf_bytes:
        out_name = f"Wooplix_Proposal_{single_stem}.pdf"
        return StreamingResponse(
            io.BytesIO(single_pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{out_name}"',
                "X-Proposal-Client": single_client,
                "X-Proposal-Filename": out_name,
                "X-Proposal-Type": "pdf",
                "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Client, X-Proposal-Filename, X-Proposal-Type",
            },
        )

    # Direct DOCX response if specifically requested
    if item_count == 1 and requested_format == "docx" and single_docx_bytes:
        out_name = f"Wooplix_Proposal_{single_stem}.docx"
        return StreamingResponse(
            io.BytesIO(single_docx_bytes),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f'attachment; filename="{out_name}"',
                "X-Proposal-Client": single_client,
                "X-Proposal-Filename": out_name,
                "X-Proposal-Type": "docx",
                "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Client, X-Proposal-Filename, X-Proposal-Type",
            },
        )

    # Full ZIP bundle (for batch of multiple files, or if requested_format == "zip")
    if item_count == 1:
        out_name = f"Wooplix_Proposal_{single_stem}.zip"
    else:
        out_name = f"Wooplix_Proposals_{item_count}_files.zip"

    return StreamingResponse(
        io.BytesIO(zip_payload),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{out_name}"',
            "X-Proposal-Client": single_client if item_count == 1 else f"{item_count} Proposals",
            "X-Proposal-Filename": out_name,
            "X-Proposal-Type": "zip",
            "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Client, X-Proposal-Filename, X-Proposal-Type",
        },
    )


@app.post("/api/brd/generate")
@app.post("/api/index.py/brd/generate")
@app.post("/brd/generate")
async def generate_brd(request: Request, reviews: str = Form(...)):
    """Dedicated Business Requirements Document agent and export path."""
    try:
        reviewed = json.loads(reviews)
        if not isinstance(reviewed, list) or not 1 <= len(reviewed) <= MAX_FILES:
            raise ValueError("Supply one to five analyzed requirements.")
        prepared = []
        for row in reviewed:
            context = workflow.unseal(row["review_token"])
            answers = row.get("answers", {})
            document, trace = await run_in_threadpool(multi_agent_graph.run_brd, context, answers)
            document = dict(document, agent_trace=trace)
            prepared.append((document, answers))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    requested_format = (request.query_params.get("format") or "pdf").lower()
    if requested_format not in {"pdf", "docx", "zip"}:
        raise HTTPException(400, "Choose PDF, Word or ZIP format.")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    files = []
    with tempfile.TemporaryDirectory(prefix="wooplix-brd-") as work:
        for index, (document, _) in enumerate(prepared, start=1):
            stem = _safe_name(document["project_name"])
            if stem == "Project_name_to_be_confirmed":
                stem = "Business_Requirements"
            root = Path(work) / f"brd-{index}"
            root.mkdir()
            pdf_path = root / f"Wooplix_Business_Requirements_{stem}.pdf"
            docx_path = root / f"Wooplix_Business_Requirements_{stem}.docx"
            json_path = root / f"Wooplix_Business_Requirements_{stem}.json"
            json_path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
            brd_agent.build_docx(document, str(docx_path))
            if requested_format == 'docx' and len(prepared) == 1:
                files.append((pdf_path, docx_path, json_path))
                continue
            try:
                if os.environ.get("VERCEL"):
                    base = (os.environ.get("PDF_RENDER_BASE_URL") or
                            os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL"))
                    token = os.environ.get("PDF_RENDER_TOKEN")
                    if not base or not token:
                        raise RuntimeError("PDF renderer is not configured")
                    url = base if base.startswith("http") else f"https://{base}"
                    response = await run_in_threadpool(requests.post,
                        f"{url.rstrip('/')}/api/pdf.php",
                        json={"html": brd_agent.build_html(document)},
                        headers={"Authorization": f"Bearer {token}"}, timeout=240,
                    )
                    response.raise_for_status()
                    pdf_path.write_bytes(response.content)
                else:
                    await run_in_threadpool(brd_agent.build_pdf, document, str(pdf_path))
            except Exception as exc:
                raise HTTPException(502, "Could not create the Business Requirements PDF.") from exc
            files.append((pdf_path, docx_path, json_path))

        if len(files) == 1 and requested_format in {"pdf", "docx"}:
            selected = files[0][0] if requested_format == "pdf" else files[0][1]
            media_type = ("application/pdf" if requested_format == "pdf" else
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            return StreamingResponse(
                io.BytesIO(selected.read_bytes()), media_type=media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{selected.name}"',
                    "X-Proposal-Filename": selected.name,
                    "X-Proposal-Client": "Business Requirements",
                    "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Filename, X-Proposal-Client",
                },
            )

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for pdf_path, docx_path, json_path in files:
                for path in (pdf_path, docx_path, json_path):
                    bundle.write(path, arcname=f"Wooplix_Business_Requirements/{path.name}")
        archive.seek(0)
        name = f"Wooplix_Business_Requirements_{len(files)}_files.zip"
        return StreamingResponse(
            archive, media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Proposal-Filename": name,
                "X-Proposal-Client": "Business Requirements",
                "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Filename, X-Proposal-Client",
            },
        )
