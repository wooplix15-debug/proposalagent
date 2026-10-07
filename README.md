# Wooplix Proposal & BRD — LangGraph Multi-Agent Edition

An internal document workspace that runs specialist agents against one
source-grounded requirement register. The local application uses **Groq,
LangGraph, SQLite, FastAPI, branded DOCX and Dompdf PDF exports**.

Repository: **https://github.com/wooplix15-debug/proposalagent**.

The existing local installation is in
`/Users/ankitapandey/Downloads/Wooplix_Proposal_Agent_LangGraph`.

## Run locally

For a fresh installation, clone the repository and install Python and Composer
dependencies. PDF rendering also needs PHP:

```bash
git clone https://github.com/wooplix15-debug/proposalagent.git
cd proposalagent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
composer install --no-interaction
cp .env.example .env
```

Set `GROQ_API_KEY` in `.env`, and optionally configure the Telegram bot as
described below. Then start the app from the project folder:

```bash
.venv/bin/uvicorn api.index:app --host 127.0.0.1 --port 8001
```

Open **http://127.0.0.1:8001/**. SQLite creates its checkpoint file automatically
at `.state/wooplix.sqlite`; there is no database server to install and no login
in the local prototype.

The existing local `.env` on this machine is already configured. Credentials,
local checkpoints, installed dependencies and generated documents are excluded
from Git. The notebook is a legacy workflow, not the multi-agent runner.

## Browser workflow

1. Select Proposal or BRD and upload one requirement file or paste text.
2. Analyze the requirement and answer the clarification questions.
3. Run the supervisor and specialist agents.
4. Review the generated draft, proposed designs, risks and open decisions.
5. Inspect **Agent workflow trace**. Advanced reviewers can edit draft JSON.
6. Accept the draft and download DOCX, PDF or a ZIP with all files and audit JSON.

The thread is saved in SQLite. Refreshing the browser restores the last saved
clarification/review stage. Re-exporting a reviewed document uses the saved
draft and does not call the model again. A changed draft is validated again.

## Visual workflow

Open **http://127.0.0.1:8001/workflow**, or select **Visual agent workflow** in
the workspace sidebar. The interactive canvas supports pan, zoom, fit and
clickable node details with source-code references.

- **Agent overview** folds related tools and control steps into role cards.
- **Exact graph nodes** displays the compiled LangGraph's 24 workflow steps
  plus Start/End and all conditional routes, including the critic packet loop.
- Load a saved thread ID, or use **Last case**, to highlight recorded steps,
  human interrupts, pending work and failed tasks. **Load / refresh** reads
  the latest checkpoint; **Architecture** clears the execution overlay.
- A direct case link is `/workflow?thread_id=CASE_ID`. Inspection reads the
  checkpoint and makes no model calls.

There are **8 main agent roles per document**: Requirement Analyst, Supervisor,
Solution Architect, Delivery Estimator, Commercial Analyst, Risk & Dependency,
the selected Writer and Quality Critic. An optional Revision Writer adds a
ninth role. Across both document routes there are **10 distinct role
implementations**, counting Proposal Writer and BRD Writer separately and
including Revision Writer. Tools and human checkpoints are not AI agents.

Approval routes back through validation and quality checking before export.
The critic reuses its result when the approved draft is unchanged.

## Telegram bot

Telegram uses the same saved cases, human-review gates and export API as the
browser. It starts automatically with the local server when configured.

1. In Telegram, open **@BotFather**, send `/newbot`, and follow its instructions.
2. Paste the provided token into this project's `.env`:

   ```dotenv
   TELEGRAM_BOT_TOKEN=your_botfather_token
   TELEGRAM_POLLING_ENABLED=1
   TELEGRAM_AGENT_BASE_URL=http://127.0.0.1:8001
   ```

3. Restart the local server using the command in **Run locally**. Keep the
   server running while using Telegram. Polling uses outbound HTTPS.
4. Open your bot and send `/start`, then `/proposal` or `/brd`. Send requirement
   text or a TXT, MD, DOC, DOCX or PDF file up to 4 MB.
5. Answer the clarification questions. `/skip` leaves the current answer open;
   `/generate` uses saved answers and leaves remaining questions open.
6. Read the full draft text attachment. Tap **Accept draft** or use `/approve`.
   Then tap **Word**, **PDF**, or **All files ZIP** to receive the reviewed export.

Other commands: `/status` restores the saved case, `/preview` resends the draft,
`/cancel` cancels draft review, `/retry` continues incomplete agent steps,
and `/new` starts another case of the selected document type.

Chat bindings, answers and polling offsets persist in `.state/telegram.sqlite`;
document checkpoints remain in `.state/wooplix.sqlite`. One current case is
bound to each private chat. Old case buttons are rejected after starting a
new case. Downloads use the saved reviewed draft rather than generating again.

`GET /api/health` includes Telegram configuration, polling status and bot
username. It never returns the token. The default accepts private chats; set
`TELEGRAM_ALLOWED_CHAT_IDS` to comma-separated private chat/user IDs to restrict
access. Run one local API process/poller per bot token. Set
`TELEGRAM_POLLING_ENABLED=0` to disable polling, and update
`TELEGRAM_AGENT_BASE_URL` if you change the server port.

## Deploy on Vercel with Neon

Import **`wooplix15-debug/proposalagent`** into a Vercel project named
`proposalagent`. Use the repository root and the **FastAPI** framework preset.
The older Vercel project named `proposal` is connected to a different repository.
Once this repository is connected, pushes to `main` trigger production deploys.

The app uses **PostgreSQL on Neon** for deployed LangGraph checkpoints,
Telegram conversations, duplicate-update records and cross-instance chat
leases. Local runs retain SQLite unless `LANGGRAPH_STORAGE=postgres` is set.
Vercel never starts a polling thread or stores cases in its temporary filesystem.

This workspace is linked to Neon project `autumn-scene-20709366`, branch
`production`. `neon.ts` intentionally declares `defineConfig({})`: Postgres is
the required Neon service. App migrations are managed in code by the official
LangGraph PostgreSQL checkpointer and `migrations/001_telegram.sql`.

### Environment variables

After `neon link` has pulled the database URLs into the private local `.env`:

```bash
.venv/bin/python make_vercel_env.py
```

This creates **`.env.vercel`** with mode `600`. Import its contents into
**Vercel → Settings → Environment Variables → Production**, then deploy.
The file contains the actual model/bot credentials and generated renderer and
webhook secrets; it is ignored by Git. Re-running preserves the generated
secrets so the app and Telegram registration stay in sync.

Required cloud settings:

| Variable | Value/purpose |
|---|---|
| `GROQ_API_KEY` | Your Groq key |
| `GROQ_MODEL` | `openai/gpt-oss-120b` |
| `DATABASE_URL` | Pooled Neon endpoint for application requests |
| `DATABASE_URL_UNPOOLED` | Direct Neon endpoint for schema setup |
| `LANGGRAPH_STORAGE` | `postgres` |
| `TELEGRAM_BOT_TOKEN` | BotFather token |
| `TELEGRAM_BOT_USERNAME` | `Proposalwooplix_bot` |
| `TELEGRAM_MODE` | `webhook` |
| `TELEGRAM_POLLING_ENABLED` | `0` |
| `TELEGRAM_WEBHOOK_SECRET` | Generated secret for Telegram's request header |
| `PDF_RENDER_TOKEN` | Generated bearer token for the PHP/Dompdf function |

Vercel supplies the production/deployment hostname automatically. If using
another host, set `APP_BASE_URL` to the public HTTPS URL. `NEON_BRANCH` is
included as workspace context but is not required by the Python runtime.

### Connect Telegram after deployment

1. Verify `https://YOUR-DEPLOYMENT/api/health` reports PostgreSQL storage and
   Telegram `webhook_ready`. This indicates configuration is present; it does
   not register the webhook automatically.
2. Stop local polling (`TELEGRAM_POLLING_ENABLED=0` in the local `.env`, then
   restart the local app) before switching the bot to its public webhook.
3. Register the URL using the same private env copy imported into Vercel:

   ```bash
   .venv/bin/python configure_telegram_webhook.py --base-url https://YOUR-DEPLOYMENT
   ```

4. Open the bot, send `/start`, and test a proposal or BRD.

`POST /api/telegram/webhook` checks Telegram's secret header, records the
update ID in PostgreSQL and schedules ASGI background processing. Duplicate
deliveries are acknowledged without repeating the action. A chat lease prevents
simultaneous instances from treating an early reply as an answer to the next
stage. Leases expire after a terminated invocation. Saved checkpoints and
`/status` / `/retry` support recovery from model failures or function timeouts.
Vercel function execution is configured for up to 300 seconds per request.

DOCX is rendered in Python. PDF uses the separate authenticated
`api/pdf.php` Dompdf function, with Composer dependencies resolved by its PHP
runtime; ZIP includes DOCX, PDF and audit JSON. The workspace is at `/` and the
interactive agent diagram is at `/workflow`.

## Multi-agent workflow

```text
Source requirement
  → Requirement analyst: source checklist and clarification gaps
  → Retrieval tool: relevant bundled delivery records
  → Shared canonical register: content-stable requirement IDs
  → Human clarification
  → Supervisor: focused tasks and quality-review priorities
  → Parallel specialists:
       Solution architect     → proposed implementation approach
       Delivery estimator     → dependencies around tool-calculated times
       Commercial analyst     → quote dependencies around unquoted categories
       Risk/dependency agent  → source-linked risks and mitigations
  → Reconciliation: schemas, source IDs, evidence IDs and product checks
  → Proposal writer OR BRD writer
  → Deterministic validation + independent model quality critic
  → One targeted repair, then re-check or reject
  → Human draft review
  → Exact saved DOCX/PDF/ZIP export
```

Specialists return typed JSON, not an agent-to-agent free-text conversation.
Each branch writes a separate state field. A fan-in barrier waits for all four
specialists before the writer runs. The source requirement register is shared
and does not change as agents work.

The supervisor, solution architect, delivery analyst, risk analyst, writer and
critic use role-specific model calls. The commercial agent runs a model call
only when commercial categories were requested. **Durations, commercial
amounts and export operations are controlled by Python tools**, not model
arithmetic. There is no approved price master bundled here, so amounts remain
unquoted. Delivery records guide estimates; they are not a capability catalogue.

## What is visible in the documents

In addition to the existing branded scope/BRD tables, multi-agent exports add:

- Solution recommendations explicitly labeled *proposed for review*
- Delivery dependencies and confirmation conditions
- Project-specific risks and planning assumptions
- Open decisions required from the client/project team
- Requirement traceability with stable IDs, source filenames and excerpts

The ZIP's JSON contains specialist findings, requirement register, quality
report and workflow trace. Source references are file/excerpt level; page-level
citations are not yet implemented. Proposed details are not client approvals.

## Main files

| File | Responsibility |
|---|---|
| `multi_agent_graph.py` | Supervisor, parallel branches, writers, critic and interrupts |
| `workflow_view.py` | Display metadata and checkpoint overlays over actual graph topology |
| `public/workflow.html` | Interactive visual workflow canvas |
| `multi_agent_specialists.py` | Role-specific structured model calls |
| `multi_agent_contracts.py` | Typed specialist finding contracts |
| `multi_agent_tools.py` | Stable source IDs, grounding, locked repairs and shared planning tables |
| `graph_runtime.py` | SQLite/PostgreSQL checkpoint selection |
| `postgres_runtime.py` | Cloud connection pool and serialized schema setup |
| `telegram_cloud.py` | PostgreSQL conversation state, webhook deduplication and chat leases |
| `telegram_bot.py` | Telegram polling, persistent conversations and reviewed downloads |
| `graph_cli.py` | Persistent terminal workflow |
| `agent_graph.py` | Shared single-workflow analysis and structural validation helpers |
| `graph_schemas.py` | Analysis/document/reviewer contracts |
| `api/index.py` | Browser, state/resume and reviewed export API |
| `public/index.html` | Persistent multi-agent review interface |
| `wooplix_agent.py` | Proposal prompts, deterministic scope, DOCX/HTML/PDF renderers |
| `business_requirements_agent.py` | BRD specification writer and renderers |
| `project_delivery_data.json` | Bundled product/project delivery data |
| `market_delivery_benchmarks.json` | Versioned planning benchmarks |
| `smoke_multiagent.py` | Real local synthetic-case generation and export check |

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Configuration and specialist information |
| `POST /api/telegram/webhook` | Authenticated Telegram webhook with durable deduplication |
| `GET /api/agent/workflow?thread_id=CASE_ID` | Actual graph topology and optional saved execution overlay |
| `POST /api/agent/start` | Start one case; multipart `text` or `files`, `document_type` |
| `POST /api/agent/resume` | Resume human input; JSON `thread_id` and `response` |
| `GET /api/agent/state/{thread_id}` | Restore the saved review state |
| `POST /api/agent/retry` | Continue incomplete steps after a model/rate-limit failure |
| `GET /api/agent/export/{thread_id}?format=docx\|pdf\|zip` | Export the reviewed checkpoint |

Clarification response: `{"answers":{"q1":"Leave open for discovery"}}`.
Draft response: `{"decision":"accept","reviewer":"Reviewer name"}` or
`{"decision":"cancel","reviewer":"Reviewer name"}`. A full edited draft can
be supplied as `edited_document`; it must still pass validation.

The original `/api/analyze`, `/api/generate` and `/api/brd/generate` routes remain
compatible. Their generation routes run the multi-agent specialists and return
a draft directly, without the persistent browser approval stage.

If Groq returns a rate limit, the browser shows **Retry saved agent steps**.
Successful nodes remain checkpointed. Retrying continues failed/pending nodes
instead of rerunning every model call. The smoke test also exercises this path.
Long drafts are reviewed by the critic in bounded text packets; each packet
has its own checkpoint to keep requests within model-provider input limits.

## Persistent CLI

```bash
.venv/bin/python graph_cli.py tests/fixtures/multiagent_requirement.txt \
  --document-type proposal --thread-id demo-proposal
```

Use `--document-type brd` for a BRD. Reuse the same thread to resume the same
case. The CLI shows draft JSON before review. Reviewed DOCX/JSON files are
written to `out/langgraph/`.

## Verification

Offline regression tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Real synthetic-case smoke test, with the local server running:

```bash
.venv/bin/python smoke_multiagent.py --document-type proposal
.venv/bin/python smoke_multiagent.py --document-type brd
```

It checks specialist execution, quality review, human-review resume and all
three export formats. Outputs go to `out/multiagent-smoke/`.

An optional real-browser check is available with Playwright and installed
Google Chrome. Use the ID of a case paused at draft review:

```bash
.venv/bin/python -m pip install playwright
.venv/bin/python browser_smoke.py --thread-id CASE_ID
```

It restores the draft, checks the specialist trace, accepts the review and
verifies the Word download. No additional model analysis is needed.

Check the visual workflow against an existing case without modifying it:

```bash
.venv/bin/python browser_workflow_smoke.py --thread-id CASE_ID
```

To check Telegram's status/preview/download path against the running API,
use an already-reviewed case. This captures deliveries locally:

```bash
.venv/bin/python telegram_smoke.py --thread-id REVIEWED_CASE_ID
```

Verify cloud persistence on a Neon test branch before applying migrations to
production. Supply an env file containing that branch's two database URLs:

```bash
.venv/bin/python postgres_smoke.py --env .env.neon-test
```

This checks checkpoint and conversation persistence after a pool restart,
cross-instance chat leases and update deduplication. It removes only its own
synthetic rows afterwards. Schema setup is idempotent and serialized using a
direct-connection advisory lock.

## Next upgrades

For wider company use: approved product/edition capabilities, a rate card,
completed-project evidence, page-level citations and task-specific evaluations.
SQLite persistence is for local development; deployed cases use PostgreSQL.

LangGraph remains a good fit for state, review interrupts and controlled
branching. Alternatives are a custom FastAPI state machine for simpler flows,
Temporal for durable long-running operations, PydanticAI for typed model tools,
Microsoft Agent Framework for Microsoft-heavy environments, or the OpenAI
Agents SDK for tool-calling integrations.
