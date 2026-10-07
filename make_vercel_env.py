"""Create a private, paste-ready Vercel env file from the local Neon setup."""
import argparse
import json
import os
from pathlib import Path
import secrets

HERE = Path(__file__).resolve().parent


def read_env(path):
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def build_env(source, existing=None):
    existing = existing or {}
    for key in ("GROQ_API_KEY", "TELEGRAM_BOT_TOKEN", "DATABASE_URL", "DATABASE_URL_UNPOOLED"):
        if not source.get(key):
            raise ValueError(f"Set {key} in the local .env before creating the Vercel copy.")
    values = {
        "GROQ_API_KEY": source["GROQ_API_KEY"],
        "GROQ_MODEL": source.get("GROQ_MODEL") or "openai/gpt-oss-120b",
        "DATABASE_URL": source["DATABASE_URL"],
        "DATABASE_URL_UNPOOLED": source["DATABASE_URL_UNPOOLED"],
        "LANGGRAPH_STORAGE": "postgres",
        "TELEGRAM_BOT_TOKEN": source["TELEGRAM_BOT_TOKEN"],
        "TELEGRAM_BOT_USERNAME": source.get("TELEGRAM_BOT_USERNAME") or "Proposalwooplix_bot",
        "TELEGRAM_MODE": "webhook",
        "TELEGRAM_POLLING_ENABLED": "0",
        "TELEGRAM_WEBHOOK_SECRET": existing.get("TELEGRAM_WEBHOOK_SECRET") or secrets.token_urlsafe(32),
        "PDF_RENDER_TOKEN": existing.get("PDF_RENDER_TOKEN") or source.get("PDF_RENDER_TOKEN") or secrets.token_hex(32),
    }
    for key, value in source.items():
        if value and (key.startswith("GROQ_API_KEY_") or key.startswith("ZOHO_") or
                      key in {"WORKFLOW_SIGNING_SECRET", "TELEGRAM_ALLOWED_CHAT_IDS", "NEON_BRANCH"}):
            values[key] = value
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=HERE / ".env")
    parser.add_argument("--output", type=Path, default=HERE / ".env.vercel")
    args = parser.parse_args()
    values = build_env(read_env(args.source), read_env(args.output))
    body = "# Paste/import into Vercel project proposalagent — Production environment.\n"
    body += "# Vercel provides its deployment URL automatically; no localhost URL is included.\n"
    body += "\n".join(f"{key}={json.dumps(value)}" for key, value in values.items()) + "\n"
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(body)
    print(f"Created {args.output} with {len(values)} variables (permissions 600).")
    print("Import this file in Vercel Settings → Environment Variables, then deploy.")


if __name__ == "__main__":
    main()
