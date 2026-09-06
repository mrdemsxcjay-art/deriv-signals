"""
Envoie le message TEST Telegram (étape 5).

Usage :
  export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
  python scripts/send_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from src.notify.telegram import TelegramError, format_test, send_html  # noqa: E402


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("❌ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants.")
        print("   export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...")
        sys.exit(1)
    with open("config/settings.yaml", "r", encoding="utf-8") as f:
        settings = yaml.safe_load(f)
    try:
        r = send_html(token, chat, format_test(settings))
    except TelegramError as e:
        print(f"❌ Échec d'envoi : {e}")
        sys.exit(1)
    print(f"✅ Message TEST envoyé (message_id={r.get('message_id')}).")


if __name__ == "__main__":
    main()
