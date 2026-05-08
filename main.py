"""
Точка входу для cron / Windows Task Scheduler.

Приклади запуску:
    python main.py collect      # зібрати курси з усіх джерел (для cron 3х на день)
    python main.py report       # згенерувати звіт (для cron 1 раз о 10:00)
    python main.py both         # зібрати + звіт одразу (ручний запуск)

Логи пишуться у fx-tracker.log поруч зі скриптом.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from analyzer import analyze, spread_analysis
from fetchers import fetch_all
from storage import init_db, latest, save_rates

LOG_PATH = Path(__file__).parent / "fx-tracker.log"
REPORT_PATH = Path(__file__).parent / "last-report.txt"

PAIRS = ["USD/UAH", "EUR/UAH", "EUR/USD"]
SOURCES = ["monobank", "raiffeisen", "sense"]


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def cmd_collect() -> int:
    init_db()
    rates = fetch_all(SOURCES)
    if not rates:
        logging.warning("Нічого не отримано з жодного джерела.")
        return 1
    n = save_rates(rates)
    logging.info(
        "Збережено %d записів з %d джерел.",
        n, len({r["source"] for r in rates}),
    )
    return 0


def _fmt_rate(v: float | None, digits: int = 4) -> str:
    return f"{v:.{digits}f}" if v is not None else "   —   "


def _fmt_pct(v: float | None) -> str:
    return f"{v:5.1f}%" if v is not None else "  —  "


def cmd_report() -> int:
    """Формує текстовий звіт і зберігає в last-report.txt."""
    init_db()
    lines: list[str] = []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append("=" * 78)
    lines.append(f"  FX-tracker звіт | {ts}")
    lines.append("=" * 78)

    # 1. Поточні курси у таблиці
    lines.append("")
    lines.append("Поточні курси:")
    lines.append(
        f"  {'Джерело':<12} {'Пара':<10} "
        f"{'Buy (ти продаєш)':>18} {'Sell (ти купуєш)':>18}"
    )
    lines.append("  " + "-" * 74)

    for pair in PAIRS:
        for src in SOURCES:
            cur = latest(src, pair)
            if cur is None:
                continue
            buy = cur.get("buy")
            sell = cur.get("sell")
            if buy is None and sell is None:
                continue
            lines.append(
                f"  {src:<12} {pair:<10} "
                f"{_fmt_rate(buy):>18} "
                f"{_fmt_rate(sell):>18}"
            )

    # 2. Аналіз по Монобанку — бо саме там твій ФОП
    lines.append("")
    lines.append("Аналіз (база — Монобанк, бо там твій ФОП):")
    lines.append("")

    for pair in ["USD/UAH", "EUR/UAH"]:
        sig = analyze("monobank", pair)
        lines.append(f"  ▸ {pair}")
        if sig.action == "insufficient_data":
            lines.append(f"    {sig.note}")
            lines.append("")
            continue
        lines.append(
            f"    Середнє за 7 днів : "
            f"buy={_fmt_rate(sig.avg_buy_7d)}  sell={_fmt_rate(sig.avg_sell_7d)}"
        )
        lines.append(
            f"    Середнє за 30 днів: "
            f"buy={_fmt_rate(sig.avg_buy_30d)}  sell={_fmt_rate(sig.avg_sell_30d)}"
        )
        lines.append(
            f"    Позиція в історії: "
            f"buy {_fmt_pct(sig.pct_rank_buy_30d)}  "
            f"sell {_fmt_pct(sig.pct_rank_sell_30d)}  "
            f"(100% = максимум за 30 днів)"
        )
        tag = {
            "sell_fx": "🟢 КОНВЕРТУВАТИ ВАЛЮТУ В ГРН",
            "buy_fx":  "🟢 КУПИТИ ВАЛЮТУ ЗА ГРН",
            "hold":    "🟡 ЧЕКАТИ",
        }.get(sig.action, "")
        lines.append(f"    Рекомендація:      {tag}")
        lines.append(f"    {sig.note}")
        lines.append("")

    # 3. Порівняння банків
    lines.append("Де найвигідніше здійснити операцію ЗАРАЗ:")
    for pair in ["USD/UAH", "EUR/UAH"]:
        sp = spread_analysis(pair, SOURCES)
        lines.append(f"  ▸ {pair}")
        if sp["best_place_to_sell_fx"]:
            bank, rate = sp["best_place_to_sell_fx"]
            lines.append(f"    Продати валюту дорожче за все: {bank} @ {rate:.4f}")
        if sp["best_place_to_buy_fx"]:
            bank, rate = sp["best_place_to_buy_fx"]
            lines.append(f"    Купити валюту дешевше за все:  {bank} @ {rate:.4f}")
        lines.append("")

    # 4. Стратегія
    lines.append("-" * 78)
    lines.append("Нагадування щодо стратегії:")
    lines.append("  • Не конвертуй всю зарплату одразу — розбий на 3-4 транші за місяць.")
    lines.append("  • Спред (різниця buy/sell) — головна втрата. Мінімізуй кількість конвертацій.")
    lines.append("  • USD↔EUR: якщо треба і те, і те, вигідніше прямий обмін USD→EUR")
    lines.append("    через крос-курс, ніж USD→UAH→EUR (подвійний спред).")
    lines.append("  • Райф і Sense часто мають кращий курс для великих сум (>$1000),")
    lines.append("    ніж Моно — перевіряй перед великою операцією.")
    lines.append("=" * 78)

    report = "\n".join(lines)
    print(report)
    REPORT_PATH.write_text(report, encoding="utf-8")
    logging.info("Звіт збережено у %s", REPORT_PATH)
    return 0


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="FX tracker для USD/EUR/UAH")
    parser.add_argument(
        "mode",
        nargs="?",
        default="both",
        choices=["collect", "report", "both"],
        help=(
            "collect — тільки зібрати дані (для cron 3х на день); "
            "report — тільки звіт (для cron о 10:00); "
            "both — і те, і те (ручний запуск)"
        ),
    )
    args = parser.parse_args()

    if args.mode in ("collect", "both"):
        rc = cmd_collect()
        if rc != 0 and args.mode == "collect":
            return rc

    if args.mode in ("report", "both"):
        return cmd_report()

    return 0


if __name__ == "__main__":
    sys.exit(main())
