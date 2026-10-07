"""Telegram polling adapter for the persistent local agent API.

Conversation bindings/answers survive restarts in SQLite. The document graph
and its approval/export rules remain owned by the existing FastAPI endpoints.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import quote
from uuid import uuid4

import requests


LOG = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
MAX_UPLOAD = 4 * 1024 * 1024
SUFFIXES = {".txt", ".md", ".doc", ".docx", ".pdf"}
HELP = (
    "Wooplix Proposal & BRD Agent\n\n"
    "/proposal — start a proposal\n/brd — start a BRD\n"
    "Then send requirement text or a TXT, MD, DOC, DOCX or PDF file (up to 4 MB).\n\n"
    "Reply to each clarification, or /skip to leave it open for discovery.\n"
    "/generate — use saved answers and leave remaining questions open\n"
    "/status — restore your saved case\n/preview — read the draft\n"
    "/approve — accept the draft\n/cancel — cancel the draft\n"
    "/word, /pdf, /zip — download the reviewed document\n"
    "/retry — continue incomplete agent steps"
)


class BotError(Exception):
    pass


class AgentAPIError(BotError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def text_chunks(text, limit=3500):
    """Telegram limits text in UTF-16 units, including supplementary emoji."""
    chunk, units = [], 0
    for char in str(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit:
            yield "".join(chunk)
            chunk, units = [], 0
        chunk.append(char)
        units += width
    if chunk:
        yield "".join(chunk)


class TelegramClient:
    def __init__(self, token):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._files = f"https://api.telegram.org/file/bot{token}/"

    def call(self, method, data=None, files=None, timeout=90):
        try:
            response = requests.post(self._base + method, data=data or {}, files=files,
                                     timeout=(10, timeout))
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            # Request exception strings contain the URL, and therefore the token.
            raise BotError(f"Telegram request failed ({type(exc).__name__}).") from None
        if not response.ok or not result.get("ok"):
            raise BotError("Telegram could not complete the request. Check bot configuration and connectivity.")
        return result.get("result")

    def message(self, chat, text, keyboard=None):
        chunks = list(text_chunks(text))
        for index, chunk in enumerate(chunks):
            body = {"chat_id": chat, "text": chunk}
            if keyboard and index == len(chunks) - 1:
                body["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
            self.call("sendMessage", body)

    def document(self, chat, content, filename, caption=""):
        self.call("sendDocument", {"chat_id": chat, "caption": caption},
                  files={"document": (Path(filename).name, content)}, timeout=120)

    def download(self, document):
        name = Path(document.get("file_name", "requirement.txt")).name
        if Path(name).suffix.lower() not in SUFFIXES:
            raise BotError("Send a TXT, MD, DOC, DOCX or PDF requirement file.")
        if document.get("file_size", 0) > MAX_UPLOAD:
            raise BotError("The requirement file must be under 4 MB.")
        info = self.call("getFile", {"file_id": document["file_id"]})
        if info.get("file_size", 0) > MAX_UPLOAD:
            raise BotError("The requirement file must be under 4 MB.")
        try:
            with requests.get(self._files + info["file_path"], stream=True, timeout=(10, 90)) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise BotError("The requirement file must be under 4 MB.")
                    chunks.append(chunk)
        except requests.RequestException as exc:
            raise BotError(f"Telegram file download failed ({type(exc).__name__}).") from None
        return name, b"".join(chunks)


class AgentAPI:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def request(self, method, path, **kwargs):
        try:
            response = requests.request(method, self.base_url + path, timeout=(10, 900), **kwargs)
        except requests.RequestException as exc:
            raise AgentAPIError(f"Local agent connection failed ({type(exc).__name__}). Use /status once the app is running.") from None
        if not response.ok:
            try:
                detail = response.json().get("detail", "The agent request failed.")
            except ValueError:
                detail = "The agent request failed."
            raise AgentAPIError(str(detail)[:1000], response.status_code)
        return response

    def state(self, thread):
        return self.request("GET", "/api/agent/state/" + quote(thread, safe="")).json()

    def start(self, thread, mode, text=None, upload=None):
        data = {"thread_id": thread, "document_type": mode}
        if text is not None:
            data["text"] = text
        files = {"files": upload} if upload else None
        return self.request("POST", "/api/agent/start", data=data, files=files).json()

    def resume(self, thread, response):
        return self.request("POST", "/api/agent/resume", json={"thread_id": thread, "response": response}).json()

    def retry(self, thread):
        return self.request("POST", "/api/agent/retry", json={"thread_id": thread}).json()

    def export(self, thread, format):
        response = self.request("GET", "/api/agent/export/" + quote(thread, safe=""), params={"format": format})
        return response.content, response.headers.get("x-proposal-filename", f"Wooplix_Document.{format}")


class ConversationStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.lock = Lock()
        with self.connection:
            self.connection.execute("CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
            self.connection.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")

    def load(self, chat):
        with self.lock:
            row = self.connection.execute("SELECT data FROM chats WHERE chat_id=?", (chat,)).fetchone()
        return json.loads(row[0]) if row else {"mode": "proposal", "thread_id": None, "answers": {}}

    def save(self, chat, state):
        with self.lock, self.connection:
            self.connection.execute("INSERT OR REPLACE INTO chats VALUES (?,?)", (chat, json.dumps(state)))

    def offset(self):
        with self.lock:
            row = self.connection.execute("SELECT value FROM metadata WHERE name='offset'").fetchone()
        return row[0] if row else 0

    def advance(self, offset):
        with self.lock, self.connection:
            self.connection.execute("INSERT OR REPLACE INTO metadata VALUES ('offset',?)", (offset,))

    def close(self):
        with self.lock:
            self.connection.close()


def button(label, action):
    return {"text": label, "callback_data": action}


def draft_text(document):
    """Readable full draft attachment, with source and planning tables preserved."""
    lines = ["WOOPLIX — DRAFT FOR REVIEW", ""]

    def render(value, depth=0):
        indent = "  " * depth
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "agent_audit":
                    continue
                lines.append(indent + str(key).replace("_", " ").title() + ":")
                render(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, list):
                    lines.append(indent + " | ".join(str(cell) for cell in child))
                elif isinstance(child, (dict, list)):
                    lines.append(indent + "•")
                    render(child, depth + 1)
                else:
                    lines.append(indent + "• " + str(child))
        elif value is not None and value != "":
            lines.append(indent + str(value))

    render(document)
    return "\n".join(lines).encode("utf-8")


class TelegramBot:
    def __init__(self, telegram, agent_api, store, allowed_chats=()):
        self.telegram, self.api, self.store = telegram, agent_api, store
        self.allowed_chats = set(allowed_chats)
        self.active, self.lock = set(), Lock()

    def reserve_update(self, update):
        """Reserve a chat before queueing so backlogged replies cannot skip stages."""
        callback = update.get("callback_query")
        message = callback.get("message", {}) if callback else update.get("message", {})
        chat = message.get("chat", {})
        user = callback.get("from", {}) if callback else message.get("from", {})
        chat_id = chat.get("id")
        if chat.get("type") != "private" or chat_id != user.get("id"):
            return
        if self.allowed_chats and chat_id not in self.allowed_chats:
            return
        if callback:
            self.telegram.call("answerCallbackQuery", {"callback_query_id": callback["id"]})
        with self.lock:
            busy = chat_id in self.active
            if not busy:
                self.active.add(chat_id)
        if busy:
            self.telegram.message(chat_id, "Your agent is still working. I'll send the next step when it finishes.")
            return
        return chat_id, user, message, callback

    def handle_update(self, update):
        context = self.reserve_update(update)
        if context:
            self.process_reserved(context)

    def process_reserved(self, context):
        chat_id, user, message, callback = context
        try:
            self._handle(chat_id, user, message, callback)
        except BotError as exc:
            self.telegram.message(chat_id, str(exc) + "\nUse /status to restore the case, or /retry for incomplete steps.")
        except Exception as exc:
            LOG.warning("Telegram conversation failed (%s)", type(exc).__name__)
            self.telegram.message(chat_id, "This step could not finish. Use /status to inspect the saved case, then /retry if needed.")
        finally:
            with self.lock:
                self.active.discard(chat_id)

    def _saved(self, chat, session):
        if not session["thread_id"]:
            raise BotError("Start with /proposal or /brd, then send your requirement.")
        try:
            return self.api.state(session["thread_id"])
        except AgentAPIError as exc:
            if exc.status == 404:
                session["thread_id"] = None
                self.store.save(chat, session)
                raise BotError("This case has no saved checkpoint yet. Send the requirement again.") from None
            raise

    def _handle(self, chat, user, message, callback):
        session = self.store.load(chat)
        text = message.get("text", "").strip()
        command, argument = "", ""
        if callback:
            parts = callback.get("data", "").split("|")
            command = parts[0]
            if command == "new" and len(parts) == 2 and parts[1] in {"proposal", "brd"}:
                command = parts[1]
            elif len(parts) < 2 or parts[1] != session["thread_id"]:
                raise BotError("That button belongs to an older case. Use /status for the current one.")
            if command == "skip":
                argument = parts[2] if len(parts) == 3 else ""
        elif text.startswith("/"):
            name, _, argument = text.partition(" ")
            command = name[1:].split("@", 1)[0].lower()
        if command in {"start", "help"}:
            self.telegram.message(chat, HELP, [[button("Proposal", "new|proposal"), button("BRD", "new|brd")]])
            return
        if command in {"proposal", "brd", "new"}:
            session = {"mode": session["mode"] if command == "new" else command, "thread_id": None, "answers": {}}
            self.store.save(chat, session)
            if not argument:
                self.telegram.message(chat, f"Send the requirement text or file for your {session['mode'].upper()}.")
                return
            text = argument
            command = ""
        if command == "status":
            self._present(chat, session, self._saved(chat, session), preview=False)
            return
        if command == "retry":
            self._saved(chat, session)
            self.telegram.message(chat, "Continuing saved agent steps…")
            self._present(chat, session, self.api.retry(session["thread_id"]))
            return
        if command in {"word", "docx", "pdf", "zip"}:
            self._saved(chat, session)
            format = "docx" if command in {"word", "docx"} else command
            content, filename = self.api.export(session["thread_id"], format)
            self.telegram.document(chat, content, filename, "Reviewed Wooplix document")
            return
        if command == "preview":
            state = self._saved(chat, session)
            document = state.get("payload", {}).get("document") or state.get("proposal") or state.get("document")
            if not document:
                raise BotError("The draft is not ready yet. Use /status to see the current step.")
            self.telegram.document(chat, draft_text(document), f"Draft_{session['mode']}.txt", "Draft for your review")
            return
        if command in {"approve", "cancel"}:
            state = self._saved(chat, session)
            if state.get("stage") == "draft_review":
                reviewer = " ".join(user.get(key, "") for key in ("first_name", "last_name")).strip() or user.get("username") or "Telegram reviewer"
                self.telegram.message(chat, "Saving your review and checking the document…")
                self._present(chat, session, self.api.resume(session["thread_id"], {
                    "decision": "accept" if command == "approve" else "cancel", "reviewer": reviewer[:200],
                }))
            elif command == "cancel" and state.get("stage") == "clarification":
                session["thread_id"] = None
                self.store.save(chat, session)
                self.telegram.message(chat, "Conversation cleared. Start another with /proposal or /brd.")
            else:
                raise BotError("Use /status. This case is not waiting for draft review.")
            return
        if command and command not in {"skip", "generate"}:
            self.telegram.message(chat, HELP)
            return
        if not session["thread_id"]:
            if command in {"skip", "generate"}:
                raise BotError("Send the requirement first.")
            upload = self.telegram.download(message["document"]) if message.get("document") else None
            if not text and not upload:
                self.telegram.message(chat, "Send requirement text or a supported document. /help lists the commands.")
                return
            session["thread_id"] = str(uuid4())
            session["answers"] = {}
            self.store.save(chat, session)  # Bind before the long-running request, including failed/retryable starts.
            self.telegram.message(chat, "Analyzing your requirement…")
            self._present(chat, session, self.api.start(session["thread_id"], session["mode"], text=None if upload else text, upload=upload))
            return
        state = self._saved(chat, session)
        if state.get("stage") != "clarification":
            self._present(chat, session, state, preview=False)
            return
        if message.get("document"):
            raise BotError("Reply to the clarification using text. Use /proposal or /brd to start a new requirement file.")
        questions = state["payload"]["questions"]
        unanswered = [q for q in questions if q["id"] not in session["answers"]]
        if command == "generate":
            session["answers"].update({q["id"]: "Leave open for discovery" for q in unanswered})
        elif unanswered:
            if callback and argument != str(len(questions) - len(unanswered)):
                raise BotError("That question was already answered. Use /status for the next question.")
            answer = "Leave open for discovery" if command == "skip" else text
            if not answer or len(answer) > 4000:
                raise BotError("Reply with 1–4,000 characters, or use /skip.")
            session["answers"][unanswered[0]["id"]] = answer
        self.store.save(chat, session)
        if any(q["id"] not in session["answers"] for q in questions):
            self._present(chat, session, state, preview=False)
        else:
            self.telegram.message(chat, "Running the supervisor, four specialists, writer and quality critic…")
            self._present(chat, session, self.api.resume(session["thread_id"], {"answers": session["answers"]}))

    def _present(self, chat, session, state, preview=True):
        thread = session["thread_id"]
        if state.get("stage") == "clarification":
            payload = state["payload"]
            unanswered = [q for q in payload["questions"] if q["id"] not in session["answers"]]
            if unanswered:
                question = unanswered[0]
                index = len(payload["questions"]) - len(unanswered)
                options = "\nOptions: " + " / ".join(question.get("options", [])) if question.get("options") else ""
                self.telegram.message(chat, f"{payload['summary']}\n\nQuestion {index+1}/{len(payload['questions'])}: {question['question']}{options}\n\nReply with your answer or /skip.",
                                      [[button("Leave open for discovery", f"skip|{thread}|{index}")]])
            else:
                self.telegram.message(chat, payload["summary"] + "\n\nAnswers are saved. Use /generate to run the specialist agents.",
                                      [[button("Generate draft", f"generate|{thread}")]])
        elif state.get("stage") == "draft_review":
            document = state["payload"]["document"]
            if preview:
                self.telegram.document(chat, draft_text(document), f"Draft_{session['mode']}.txt", "Full draft for review")
            summary = document.get("project_introduction") or document.get("summary") or "The multi-agent draft is ready."
            self.telegram.message(chat, f"DRAFT FOR REVIEW\n{str(summary)[:1800]}\n\nRead the attached draft (/preview to get it again), then accept or cancel.\nCase: {thread}",
                                  [[button("Accept draft", f"approve|{thread}"), button("Cancel", f"cancel|{thread}")]])
        elif state.get("status") == "reviewed":
            self.telegram.message(chat, f"Reviewed {session['mode'].upper()} ready. Choose a download format.\nCase: {thread}",
                                  [[button("Word", f"docx|{thread}"), button("PDF", f"pdf|{thread}"), button("All files ZIP", f"zip|{thread}")]])
        elif state.get("status") == "cancelled":
            self.telegram.message(chat, "Draft cancelled. Start another with /proposal or /brd.")
        else:
            trace = "\n".join(state.get("trace", [])[-8:])
            self.telegram.message(chat, f"Case: {thread}\nStatus: {state.get('status', 'in progress')}\n{trace}" +
                                  ("\nUse /retry to continue saved steps." if state.get("retryable") else "\nUse /status to refresh."))


class PollingService:
    def __init__(self, bot):
        self.bot = bot
        self.stop_event = Event()
        self.thread = Thread(target=self._poll, name="wooplix-telegram", daemon=True)
        self.workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="telegram-case")
        self.status, self.username = "starting", None

    def start(self):
        self.thread.start()

    def _work(self, context):
        try:
            self.bot.process_reserved(context)
        except Exception as exc:
            LOG.warning("Telegram delivery failed (%s)", type(exc).__name__)

    def _poll(self):
        configured = False
        while not self.stop_event.is_set():
            try:
                if not configured:
                    self.username = self.bot.telegram.call("getMe").get("username")
                    self.bot.telegram.call("setMyCommands", {"commands": json.dumps([
                        {"command": command, "description": description}
                        for command, description in [("start", "Help and document choices"), ("proposal", "Start a proposal"),
                            ("brd", "Start a BRD"), ("status", "Restore saved case"), ("skip", "Leave answer open"),
                            ("generate", "Run specialist agents"), ("preview", "Read draft"), ("approve", "Accept draft"),
                            ("cancel", "Cancel draft"), ("word", "Download reviewed Word"), ("pdf", "Download reviewed PDF"),
                            ("zip", "Download all reviewed files"), ("retry", "Continue saved steps")]
                    ])})
                    configured = True
                self.status = "polling"
                updates = self.bot.telegram.call("getUpdates", {"offset": self.bot.store.offset(), "timeout": 25,
                    "allowed_updates": json.dumps(["message", "callback_query"])}, timeout=35)
                for update in updates:
                    if self.stop_event.is_set():
                        break
                    context = self.bot.reserve_update(update)
                    if context:
                        self.workers.submit(self._work, context)
                    self.bot.store.advance(update["update_id"] + 1)
            except Exception as exc:
                self.status = "connection_error"
                LOG.warning("Telegram polling unavailable (%s)", type(exc).__name__)
                self.stop_event.wait(5)
        self.status = "stopped"

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=40)
        self.workers.shutdown(wait=True, cancel_futures=True)
        self.bot.store.close()


def start_configured_bot():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or os.environ.get("TELEGRAM_POLLING_ENABLED", "1").lower() in {"0", "false", "no"}:
        return None
    state_path = Path(os.environ.get("TELEGRAM_STATE_PATH") or ".state/telegram.sqlite")
    if not state_path.is_absolute():
        state_path = HERE / state_path
    allowed = [int(chat.strip()) for chat in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if chat.strip()]
    bot = TelegramBot(TelegramClient(token), AgentAPI(os.environ.get("TELEGRAM_AGENT_BASE_URL") or "http://127.0.0.1:8001"),
                      ConversationStore(state_path), allowed)
    service = PollingService(bot)
    service.start()
    return service
