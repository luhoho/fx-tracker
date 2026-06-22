"""
Модуль збору курсів валют.

Джерела:
- Monobank: публічний API https://api.monobank.ua/bank/currency
- Raiffeisen Bank Aval: парсинг сторінки minfin.com.ua
- Sense Bank: парсинг сторінки minfin.com.ua (URL під /company/alfa-bank/)
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Iterable

import requests

log = logging.getLogger(__name__)

CCY = {980: "UAH", 840: "USD", 978: "EUR"}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}


def _http_get(url: str, timeout: int = 20) -> requests.Response:
    r = requests.get(url, headers=_HEADERS, timeout=timeout)
    if r.status_code == 429:
        raise RuntimeError(f"Rate limited by {url}. Спробуй пізніше.")
    r.raise_for_status()
    return r


def fetch_monobank() -> list[dict]:
    data = _http_get("https://api.monobank.ua/bank/currency").json()
    now = int(time.time())
    out: list[dict] = []
    for row in data:
        a = CCY.get(row.get("currencyCodeA"))
        b = CCY.get(row.get("currencyCodeB"))
        if not a or not b:
            continue
        pair = f"{a}/{b}"
        if pair not in {"USD/UAH", "EUR/UAH", "EUR/USD"}:
            continue
        buy = row.get("rateBuy")
        sell = row.get("rateSell")
        cross = row.get("rateCross")
        if buy is None and sell is None and cross is not None:
            buy = sell = cross
        out.append({
            "source": "monobank",
            "pair": pair,
            "buy": buy,
            "sell": sell,
            "cross": cross,
            "ts": now,
        })
    return out


# Регулярка терпима до переносів рядків, пробілів і <span> всередині <td>.
# Реальна розмітка minfin:
#   <td>USD</td>
#   <td>
#       43.8000
#       <span class="green per" title="...">+0.22</span>
#   </td>
_ROW_PATTERN_HTML = re.compile(
    r"<tr[^>]*>\s*"
    r"<td[^>]*>\s*(?P<ccy>USD|EUR)\s*</td>\s*"
    r"<td[^>]*>\s*(?P<buy>\d+[.,]\d+)\s*(?:<span[^>]*>.*?</span>\s*)?</td>\s*"
    r"<td[^>]*>\s*(?P<sell>\d+[.,]\d+)\s*(?:<span[^>]*>.*?</span>\s*)?</td>",
    re.IGNORECASE | re.DOTALL,
)

# Markdown-запасний патерн (на випадок інакшої форми відповіді)
_ROW_PATTERN_MD = re.compile(
    r"\|\s*(?P<ccy>USD|EUR)\s*\|\s*"
    r"(?P<buy>\d+[.,]\d+)(?:\s+[-+]?\d+(?:[.,]\d+)?)?\s*\|\s*"
    r"(?P<sell>\d+[.,]\d+)(?:\s+[-+]?\d+(?:[.,]\d+)?)?\s*\|",
    re.IGNORECASE,
)

_MINFIN_URLS = {
    "raiffeisen": "https://minfin.com.ua/company/aval/currency/",
    "sense":      "https://minfin.com.ua/company/alfa-bank/currency/",
    "pumb":       "https://minfin.com.ua/company/pumb/currency/",
    "otp":        "https://minfin.com.ua/company/otp-bank/currency/",
}


def _parse_minfin_page(html: str) -> dict[str, tuple[float, float]]:
    found: dict[str, tuple[float, float]] = {}

    def _maybe_add(ccy: str, buy_s: str, sell_s: str) -> None:
        if ccy in found:
            return
        try:
            buy = float(buy_s.replace(",", "."))
            sell = float(sell_s.replace(",", "."))
        except ValueError:
            return
        if not (5 < buy < 200 and 5 < sell < 200):
            return
        if sell < buy:
            return
        found[ccy] = (buy, sell)

    for m in _ROW_PATTERN_HTML.finditer(html):
        _maybe_add(m.group("ccy").upper(), m.group("buy"), m.group("sell"))
    for m in _ROW_PATTERN_MD.finditer(html):
        _maybe_add(m.group("ccy").upper(), m.group("buy"), m.group("sell"))
    return found


def _fetch_minfin(source_name: str, url: str) -> list[dict]:
    html = _http_get(url, timeout=30).text
    pairs = _parse_minfin_page(html)
    if not pairs:
        raise RuntimeError(
            f"Не вдалося розпарсити {url}. Можливо, minfin змінив розмітку."
        )
    now = int(time.time())
    out: list[dict] = []
    for ccy, (buy, sell) in pairs.items():
        out.append({
            "source": source_name,
            "pair": f"{ccy}/UAH",
            "buy": buy,
            "sell": sell,
            "cross": None,
            "ts": now,
        })
    return out


def fetch_raiffeisen() -> list[dict]:
    return _fetch_minfin("raiffeisen", _MINFIN_URLS["raiffeisen"])


def fetch_sense() -> list[dict]:
    return _fetch_minfin("sense", _MINFIN_URLS["sense"])


def fetch_pumb() -> list[dict]:
    """Курси ПУМБ (через minfin, безготівковий/онлайн курс банку)."""
    return _fetch_minfin("pumb", _MINFIN_URLS["pumb"])


def fetch_otp() -> list[dict]:
    """Курси ОТП Банку (через minfin, безготівковий/онлайн курс банку)."""
    return _fetch_minfin("otp", _MINFIN_URLS["otp"])


def fetch_privatbank() -> list[dict]:
    """
    Курси ПриватБанку через публічний API (безготівковий, coursid=11).
    coursid=5  — готівковий курс (каса відділення)
    coursid=11 — безготівковий / онлайн (обмін у застосунку) — наш випадок
    """
    data = _http_get(
        "https://api.privatbank.ua/p24api/pubinfo?json&exchange&coursid=11"
    ).json()
    now = int(time.time())
    out: list[dict] = []
    for row in data:
        ccy = row.get("ccy")
        base = row.get("base_ccy")
        if ccy not in ("USD", "EUR") or base != "UAH":
            continue
        try:
            buy = float(row["buy"])
            sell = float(row["sale"])
        except (KeyError, ValueError, TypeError):
            continue
        out.append({
            "source": "privatbank",
            "pair": f"{ccy}/UAH",
            "buy": buy,
            "sell": sell,
            "cross": None,
            "ts": now,
        })
    return out


def fetch_all(
    sources: Iterable[str] = (
        "monobank", "raiffeisen", "sense", "pumb", "otp", "privatbank", "nbu",
    ),
) -> list[dict]:
    from nbu import fetch as _fetch_nbu
    fns = {
        "monobank":   fetch_monobank,
        "raiffeisen": fetch_raiffeisen,
        "sense":      fetch_sense,
        "pumb":       fetch_pumb,
        "otp":        fetch_otp,
        "privatbank": fetch_privatbank,
        "nbu":        _fetch_nbu,
    }
    results: list[dict] = []
    for name in sources:
        fn = fns.get(name)
        if fn is None:
            log.warning("Невідоме джерело: %s", name)
            continue
        try:
            results.extend(fn())
            log.info("OK: %s", name)
        except Exception as e:
            log.error("Помилка з %s: %s", name, e)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    rates = fetch_all()
    print(f"\nОтримано записів: {len(rates)}")
    for r in rates:
        ts = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%H:%M:%S")
        buy = r["buy"] if r["buy"] is not None else float("nan")
        sell = r["sell"] if r["sell"] is not None else float("nan")
        print(
            f"  [{ts}] {r['source']:10s} {r['pair']:8s} "
            f"buy={buy:.4f}  sell={sell:.4f}"
        )
