"""Register the deployed webhook after the user provides the public Vercel URL."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import requests

from make_vercel_env import HERE, read_env
from telegram_bot import TelegramClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--env", type=Path, default=HERE / ".env.vercel")
    args = parser.parse_args()
    url = args.base_url.rstrip("/")
    if urlparse(url).scheme != "https" or not urlparse(url).netloc:
        raise ValueError("Supply the public HTTPS deployment URL.")
    values = read_env(args.env)
    if not values.get("TELEGRAM_BOT_TOKEN") or not values.get("TELEGRAM_WEBHOOK_SECRET"):
        raise ValueError("The private env file must contain the Telegram token and webhook secret.")
    response = requests.get(url + "/api/health", timeout=30)
    response.raise_for_status()
    health = response.json()
    if health.get("storage", {}).get("backend") != "postgres" or not health.get("storage", {}).get("configured"):
        raise ValueError("Configure the production PostgreSQL environment before connecting Telegram.")
    if health.get("telegram", {}).get("status") != "webhook_ready":
        raise ValueError("Configure the Telegram webhook environment and redeploy first.")
    client = TelegramClient(values["TELEGRAM_BOT_TOKEN"])
    client.call("setWebhook", {"url": url + "/api/telegram/webhook",
                              "secret_token": values["TELEGRAM_WEBHOOK_SECRET"],
                              "max_connections": 1,
                              "allowed_updates": json.dumps(["message", "callback_query"])})
    info = client.call("getWebhookInfo")
    if info.get("url") != url + "/api/telegram/webhook":
        raise RuntimeError("Telegram did not register the requested URL.")
    print("Telegram webhook registered:", info["url"])
    print("Pending updates:", info.get("pending_update_count", 0))


if __name__ == "__main__":
    main()
