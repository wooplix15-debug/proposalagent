"""Check Telegram adapter downloads through the running API with a saved case.

Telegram deliveries are captured in memory, so no bot token is required.
The existing case must already be reviewed; its graph state is not changed.
"""
import argparse
import io
import zipfile

from docx import Document

from telegram_bot import AgentAPI, ConversationStore, TelegramBot


class CaptureTelegram:
    def __init__(self):
        self.messages, self.documents = [], []

    def message(self, chat, text, keyboard=None):
        self.messages.append(text)

    def document(self, chat, content, filename, caption=""):
        self.documents.append((filename, content))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    api = AgentAPI(args.base_url)
    before = api.state(args.thread_id)
    if before.get("status") != "reviewed":
        raise ValueError("Use an already-reviewed case for this download smoke test.")
    telegram, store = CaptureTelegram(), ConversationStore(":memory:")
    try:
        store.save(101, {"mode": before["document_type"], "thread_id": args.thread_id, "answers": {}})
        bot = TelegramBot(telegram, api, store)
        for command in ("/status", "/preview", "/word", "/pdf", "/zip"):
            bot.handle_update({"message": {"chat": {"id": 101, "type": "private"},
                                          "from": {"id": 101}, "text": command}})
        assert len(telegram.documents) == 4, "Preview or export failed: " + telegram.messages[-1]
        outputs = {filename.rsplit(".", 1)[-1]: content for filename, content in telegram.documents}
        assert b"DRAFT FOR REVIEW" in outputs["txt"]
        assert Document(io.BytesIO(outputs["docx"])).paragraphs
        assert outputs["pdf"].startswith(b"%PDF")
        with zipfile.ZipFile(io.BytesIO(outputs["zip"])) as bundle:
            assert {name.rsplit(".", 1)[-1] for name in bundle.namelist()} >= {"docx", "pdf", "json"}
        assert api.state(args.thread_id) == before, "Inspection/download must not change the saved case."
        print("Telegram adapter with local API: saved status, draft preview, DOCX, PDF and ZIP passed.")
        print("Deliveries were captured locally; a configured bot token is required for live Telegram.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
