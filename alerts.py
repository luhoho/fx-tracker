"""
Модуль алертів для FX-tracker.

Три типи операцій:
  sell_fx  — виводиш валюту з ФОП у гривню (хочеш MAX buy-курс)
  buy_usd  — купуєш USD за UAH (хочеш MIN sell-курс)
  buy_eur  — купуєш EUR за UAH (хочеш MIN sell-курс)

Score 0–100. Якщо score >= 60 і не було алерту за останні COOLDOWN годин —
відправляємо Telegram-повідомлення.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from analyzer import spread_analysis
from notifier import send_telegram
from storage import init_db, last_alert_ts, latest, log_alert, ma_history

log = logging.getLogger(__name__)

ALERT_COOLDOWN_HOURS = 4
ALERT_TRIGGER_SCORE  = 60

# Джерела для пошуку best_bank (має відповідати SOURCES з main.py)
SOURCES = ["monobank", "raiffeisen", "sense", "pumb", "otp", "privatbank"]

Operation = Literal["sell_fx", "buy_usd", "buy_eur"]


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def calc_score(pair: str, operation: Operation) -> dict:
    """
    Розраховує score 0–100 для заданої пари та операції.

    Повертає dict з усіма компонентами для формування повідомлення.
    """
    is_sell = operation == "sell_fx"
    column  = "buy" if is_sell else "sell"

    # --- Best bank для операції ---
    sp = spread_analysis(pair, SOURCES)
    if is_sell:
        best = sp["best_place_to_sell_fx"]   # (bank, rate) | None
    else:
        best = sp["best_place_to_buy_fx"]    # (bank, rate) | None

    if best is None:
        return _empty_score(pair, operation, "Немає даних для best_bank")

    best_bank, rate = best

    # --- MA-14 ---
    ma_data = ma_history(best_bank, pair, column, days=14)
    if ma_data is None:
        # Fallback: спробуємо monobank
        ma_data = ma_history("monobank", pair, column, days=14)
    if ma_data is None:
        return _empty_score(pair, operation, "Замало даних для MA-14")

    ma14        = ma_data["ma"]
    day_high    = ma_data["day_high"]
    day_low     = ma_data["day_low"]

    # Відхилення від MA14
    deviation_pct = (rate - ma14) / ma14 * 100 if ma14 else 0.0

    # Позиція в денному діапазоні (0% = денний мінімум, 100% = максимум)
    day_range = day_high - day_low
    if day_range > 0:
        day_pos_pct = (rate - day_low) / day_range * 100
    else:
        day_pos_pct = 50.0  # якщо даних за день мало — нейтральна позиція

    # --- НБУ (еталон) ---
    nbu_cur = latest("nbu", pair)
    nbu_rate = nbu_cur["buy"] if nbu_cur else None
    spread_pct = (rate - nbu_rate) / nbu_rate * 100 if nbu_rate else None

    # --- Компоненти score ---
    if is_sell:
        # Продаємо валюту → хочемо MAX buy-курс
        # comp1: відхилення від MA14 (вага 40)
        comp1 = _clamp(deviation_pct * 40, 0, 40)

        # comp2: спред від НБУ (вага 30)
        # spread_pct типово від -3% до -0.5%; -0.5% = дуже добре
        if spread_pct is not None:
            comp2 = _clamp((spread_pct + 3) / 2.5 * 30, 0, 30)
        else:
            comp2 = 15.0  # нейтральне значення

        # comp3: позиція в денному діапазоні (вага 30)
        comp3 = day_pos_pct / 100 * 30

    else:
        # Купуємо валюту → хочемо MIN sell-курс
        # comp1: відхилення від MA14 (вага 40) — нижчий = краще
        comp1 = _clamp(-deviation_pct * 40, 0, 40)

        # comp2: спред від НБУ (вага 30)
        if spread_pct is not None:
            comp2 = _clamp((-spread_pct - 0.5) / 2.5 * 30, 0, 30)
        else:
            comp2 = 15.0

        # comp3: позиція в денному діапазоні — хочемо LOW
        comp3 = (1 - day_pos_pct / 100) * 30

    score = int(round(comp1 + comp2 + comp3))

    return {
        "score":         score,
        "trigger":       score >= ALERT_TRIGGER_SCORE,
        "rate":          rate,
        "best_bank":     best_bank,
        "ma14":          ma14,
        "deviation_pct": deviation_pct,
        "day_pos_pct":   day_pos_pct,
        "nbu_rate":      nbu_rate,
        "spread_pct":    spread_pct,
        "operation":     operation,
        "pair":          pair,
        # компоненти для діагностики
        "_comp1": comp1,
        "_comp2": comp2,
        "_comp3": comp3,
    }


def _empty_score(pair: str, operation: str, reason: str) -> dict:
    log.warning("calc_score(%s, %s): %s", pair, operation, reason)
    return {
        "score": 0, "trigger": False, "rate": None, "best_bank": None,
        "ma14": None, "deviation_pct": None, "day_pos_pct": None,
        "nbu_rate": None, "spread_pct": None,
        "operation": operation, "pair": pair,
        "_reason": reason,
    }


# ---------------------------------------------------------------------------
# Шаблони повідомлень
# ---------------------------------------------------------------------------

def _score_emoji(score: int) -> str:
    if score >= 80:
        return "🟢🟢 ДУЖЕ ВИГІДНО"
    if score >= 60:
        return "🟢 ВИГІДНО"
    if score >= 40:
        return "🟡 НЕЙТРАЛЬНО"
    return "🔴 НЕВИГІДНО"


def _amount_hint(score: int) -> str:
    if score >= 80:
        return "1500–2000"
    if score >= 70:
        return "1000–1500"
    return "500–1000"


TEMPLATES: dict[str, str] = {
    "sell_fx": (
        "💰 ВИВІД З ФОП: {pair}\n"
        "─────────────────────\n"
        "Курс:    {rate:.2f} UAH  ({best_bank})\n"
        "MA-14:   {ma14:.2f} UAH  ({deviation_sign}{deviation_pct:.1f}%)\n"
        "День:    позиція {day_pos_pct:.0f}%  (100% = денний max)\n"
        "НБУ:     {nbu_str}\n"
        "─────────────────────\n"
        "Score:   {score}/100  {score_emoji}\n"
        "Рекомендовано: ${amount_hint}"
    ),
    "buy_usd": (
        "💵 КУПИТИ USD: {pair}\n"
        "─────────────────────\n"
        "Курс sell: {rate:.2f} UAH  ({best_bank})\n"
        "MA-14:     {ma14:.2f} UAH  ({deviation_sign}{deviation_pct:.1f}%)\n"
        "День:      позиція {day_pos_pct:.0f}%  (0% = денний min)\n"
        "НБУ:       {nbu_str}\n"
        "─────────────────────\n"
        "Score:   {score}/100  {score_emoji}"
    ),
    "buy_eur": (
        "💶 КУПИТИ EUR: {pair}\n"
        "─────────────────────\n"
        "Курс sell: {rate:.2f} UAH  ({best_bank})\n"
        "MA-14:     {ma14:.2f} UAH  ({deviation_sign}{deviation_pct:.1f}%)\n"
        "День:      позиція {day_pos_pct:.0f}%  (0% = денний min)\n"
        "НБУ:       {nbu_str}\n"
        "─────────────────────\n"
        "Score:   {score}/100  {score_emoji}"
    ),
}


def _format_message(data: dict) -> str:
    operation    = data["operation"]
    score        = data["score"]
    deviation_pct = data["deviation_pct"] or 0.0

    nbu_rate = data.get("nbu_rate")
    spread_pct = data.get("spread_pct")
    if nbu_rate is not None and spread_pct is not None:
        nbu_str = f"{nbu_rate:.2f} → спред {spread_pct:+.1f}%"
    else:
        nbu_str = "—"

    return TEMPLATES[operation].format(
        pair          = data["pair"],
        rate          = data["rate"],
        best_bank     = data["best_bank"],
        ma14          = data["ma14"],
        deviation_sign= "+" if deviation_pct >= 0 else "",
        deviation_pct = abs(deviation_pct),
        day_pos_pct   = data["day_pos_pct"] or 50.0,
        nbu_str       = nbu_str,
        score         = score,
        score_emoji   = _score_emoji(score),
        amount_hint   = _amount_hint(score),
    )


# ---------------------------------------------------------------------------
# Основна логіка
# ---------------------------------------------------------------------------

PAIR_FOR_OP: dict[str, str] = {
    "sell_fx": "USD/UAH",
    "buy_usd": "USD/UAH",
    "buy_eur": "EUR/UAH",
}


def run_alerts(
    operations: list[str] | None = None,
    silent_if_no_trigger: bool = False,
) -> int:
    """
    Перевіряє score для кожної операції та відправляє Telegram-повідомлення
    якщо score >= 60 і минуло більше COOLDOWN годин від попереднього алерту.

    silent_if_no_trigger=True: не логує нічого якщо score < 60 (для виклику з collect).
    Повертає 0 завжди (помилки логуються, але не зупиняють процес).
    """
    init_db()

    if operations is None:
        operations = ["sell_fx", "buy_usd", "buy_eur"]

    now = int(time.time())

    for op in operations:
        pair = PAIR_FOR_OP.get(op)
        if pair is None:
            log.warning("Невідома операція: %s", op)
            continue

        data = calc_score(pair, op)  # type: ignore[arg-type]

        if data["rate"] is None:
            log.info("alert(%s %s): немає даних, пропускаємо", op, pair)
            continue

        score   = data["score"]
        trigger = data["trigger"]
        rate    = data["rate"]

        if not trigger:
            if not silent_if_no_trigger:
                log.info(
                    "alert(%s %s): score=%d — нижче порогу (%d), без відправки",
                    op, pair, score, ALERT_TRIGGER_SCORE,
                )
            log_alert(now, op, pair, score, rate, sent=0)
            continue

        # Перевіряємо cooldown
        last_ts = last_alert_ts(op, pair)
        if last_ts is not None:
            elapsed_hours = (now - last_ts) / 3600
            if elapsed_hours < ALERT_COOLDOWN_HOURS:
                log.info(
                    "alert(%s %s): score=%d ✓ але cooldown ще %.1f год",
                    op, pair, score, ALERT_COOLDOWN_HOURS - elapsed_hours,
                )
                continue

        # Формуємо і відправляємо
        text = _format_message(data)
        sent = send_telegram(text)

        log_alert(now, op, pair, score, rate, sent=1 if sent else 0)

        if sent:
            log.info(
                "alert(%s %s): score=%d — відправлено в Telegram ✓",
                op, pair, score,
            )
        else:
            log.warning(
                "alert(%s %s): score=%d — Telegram не налаштований або помилка",
                op, pair, score,
            )

    return 0
