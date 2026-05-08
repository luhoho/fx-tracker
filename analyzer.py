"""
Аналіз курсів і формування рекомендацій.

Підхід: не прогнозуємо майбутнє (це неможливо надійно), а показуємо,
як ПОТОЧНИЙ курс виглядає відносно середнього за останні N днів.

Ключові метрики:
- Percentile rank (де сьогоднішня ціна в розподілі за N днів)
- Відхилення від середньої (%)
- Спред між джерелами (arbitrage signal: де вигідніше)

Стратегія, яку ми підтримуємо:
1. USD → UAH (продаж доларів з ФОП): вигідно, коли rateBuy ВИЩИЙ за середній.
2. UAH → USD/EUR (купівля валюти): вигідно, коли rateSell НИЖЧИЙ за середній.
3. Порівнюємо спред між банками — купувати там, де rateSell менший.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from storage import history, latest, stats


@dataclass
class Signal:
    """Структура оцінки одного моменту для однієї пари."""
    source: str
    pair: str
    current_buy: float | None
    current_sell: float | None
    avg_buy_7d: float | None
    avg_buy_30d: float | None
    avg_sell_7d: float | None
    avg_sell_30d: float | None
    # Позиція в історії 30 днів у відсотках (0 = мін, 100 = макс)
    pct_rank_buy_30d: float | None
    pct_rank_sell_30d: float | None
    action: Literal["sell_fx", "buy_fx", "hold", "insufficient_data"]
    note: str


def percentile_rank(series: list[float], value: float) -> float | None:
    """
    Який відсоток історичних значень ≤ поточного.
    100 = поточне значення — максимум за період;
    0   = поточне — мінімум.
    """
    if not series:
        return None
    below_or_equal = sum(1 for v in series if v <= value)
    return round(100.0 * below_or_equal / len(series), 1)


def analyze(source: str, pair: str) -> Signal:
    """
    Формує сигнал по парі в одному джерелі.

    Логіка дії:
    - Якщо rateBuy (банк купує валюту) зараз у топ-25% за 30 днів → sell_fx
      (хороший момент конвертувати USD у UAH).
    - Якщо rateSell (банк продає валюту) зараз у нижніх 25% за 30 днів → buy_fx
      (хороший момент купити USD/EUR за UAH).
    - Інакше → hold.

    Пороги 25% — емпіричні; за бажанням легко змінити.
    """
    cur = latest(source, pair)
    s7 = stats(source, pair, 7)
    s30 = stats(source, pair, 30)
    hist30 = history(source, pair, 30)

    if cur is None or s30 is None or s30["n"] < 5:
        return Signal(
            source=source, pair=pair,
            current_buy=None, current_sell=None,
            avg_buy_7d=None, avg_buy_30d=None,
            avg_sell_7d=None, avg_sell_30d=None,
            pct_rank_buy_30d=None, pct_rank_sell_30d=None,
            action="insufficient_data",
            note="Замало даних (<5 точок за 30 днів). Почекай, поки накопичиться історія.",
        )

    buys = [h["buy"] for h in hist30 if h["buy"] is not None]
    sells = [h["sell"] for h in hist30 if h["sell"] is not None]

    pr_buy = percentile_rank(buys, cur["buy"]) if cur["buy"] is not None else None
    pr_sell = percentile_rank(sells, cur["sell"]) if cur["sell"] is not None else None

    # Вирішуємо дію
    action: Literal["sell_fx", "buy_fx", "hold", "insufficient_data"] = "hold"
    note = ""

    if pr_buy is not None and pr_buy >= 75:
        action = "sell_fx"
        note = (
            f"Банк купує валюту за {cur['buy']:.4f} — це в топ-{100 - int(pr_buy)}% "
            f"за 30 днів. Хороший момент конвертувати валюту в гривню."
        )
    elif pr_sell is not None and pr_sell <= 25:
        action = "buy_fx"
        note = (
            f"Банк продає валюту за {cur['sell']:.4f} — це в нижніх "
            f"{int(pr_sell)}% за 30 днів. Хороший момент купити валюту."
        )
    else:
        note = (
            f"Курс близький до середнього за 30 днів. "
            f"Поточна позиція: buy у {pr_buy}%-перцентилі, sell у {pr_sell}%-перцентилі."
        )

    return Signal(
        source=source,
        pair=pair,
        current_buy=cur["buy"],
        current_sell=cur["sell"],
        avg_buy_7d=s7["avg_buy"] if s7 else None,
        avg_buy_30d=s30["avg_buy"],
        avg_sell_7d=s7["avg_sell"] if s7 else None,
        avg_sell_30d=s30["avg_sell"],
        pct_rank_buy_30d=pr_buy,
        pct_rank_sell_30d=pr_sell,
        action=action,
        note=note,
    )


def best_bank_to_buy(pair: str, sources: list[str]) -> tuple[str, float] | None:
    """
    Знаходить банк з найнижчим rateSell (де найдешевше КУПИТИ валюту).
    Повертає (source, rate).
    """
    candidates = []
    for src in sources:
        cur = latest(src, pair)
        if cur and cur.get("sell") is not None:
            candidates.append((src, cur["sell"]))
    if not candidates:
        return None
    return min(candidates, key=lambda x: x[1])


def best_bank_to_sell(pair: str, sources: list[str]) -> tuple[str, float] | None:
    """
    Знаходить банк з найвищим rateBuy (де найдорожче ПРОДАТИ валюту).
    Повертає (source, rate).
    """
    candidates = []
    for src in sources:
        cur = latest(src, pair)
        if cur and cur.get("buy") is not None:
            candidates.append((src, cur["buy"]))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[1])


def spread_analysis(pair: str, sources: list[str]) -> dict:
    """
    Порівняння джерел: різниця між найкращим buy і найкращим sell.
    Якщо ти одночасно продаєш USD у банку А і купуєш EUR у банку Б,
    менший сумарний спред = менша втрата.
    """
    best_sell = best_bank_to_sell(pair, sources)  # найвищий buy
    best_buy = best_bank_to_buy(pair, sources)    # найнижчий sell
    return {
        "pair": pair,
        "best_place_to_sell_fx": best_sell,  # (bank, rate)
        "best_place_to_buy_fx": best_buy,
        "internal_spread": (
            best_buy[1] - best_sell[1]
            if best_buy and best_sell
            else None
        ),
    }
