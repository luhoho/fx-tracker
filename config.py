"""
Конфігурація з environment variables.
Встанови перед запуском:
    export TELEGRAM_BOT_TOKEN="123456:ABC..."
    export TELEGRAM_CHAT_ID="-1001234567890"

Або додай у .env і завантажуй через python-dotenv (не обов'язково).
"""

import os

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")
