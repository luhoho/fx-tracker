"""
Відправка Telegram-повідомлень.

Токен і chat_id беруться з env-змінних через config.py:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import logging

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)


def send_telegram(text: str) -> bool:
    """
    Відправляє текстове повідомлення в Telegram.
    Повертає True якщо успішно, False якщо не налаштований або помилка.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram не налаштований — пропускаємо відправку")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if not resp.ok:
            log.error("Telegram API помилка: %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        log.error("Помилка відправки в Telegram: %s", e)
        return False
