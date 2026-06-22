"""
Fetcher для офіційного курсу НБУ (mid-rate).

Endpoint: GET https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json
НБУ публікує курс один раз на день (~9:00 Київ).
buy == sell == rate (офіційний курс, не ринковий).

Використання:
    from nbu import fetch as fetch_nbu
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

SOURCE = "nbu"
PAIRS_MAP = {"USD": "USD/UAH", "EUR": "EUR/UAH"}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}


def fetch() -> list[dict]:
    """
    Отримує офіційний курс НБУ. Повертає список dict з ключами:
    ts (unix int), source, pair, buy, sell, cross=None.

    buy == sell == офіційний rate (не ринковий).
    """
    url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("Помилка отримання курсу НБУ: %s", e)
        return []

    now = int(time.time())
    out: list[dict] = []

    for row in data:
        cc = row.get("cc", "")
        pair = PAIRS_MAP.get(cc)
        if pair is None:
            continue
        try:
            rate = float(row["rate"])
        except (KeyError, ValueError, TypeError):
            continue

        out.append({
            "source": SOURCE,
            "pair":   pair,
            "buy":    rate,
            "sell":   rate,
            "cross":  None,
            "ts":     now,
        })

    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    rates = fetch()
    print(f"НБУ: отримано {len(rates)} записів")
    for r in rates:
        print(f"  {r['pair']}: {r['buy']:.4f}")
