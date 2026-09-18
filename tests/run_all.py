#!/usr/bin/env python3
"""Прогон всех тестов без pytest.

Обычный способ — ``pytest``. Этот раннер нужен там, где pytest не установлен
(например, в чистом контейнере или на машине без dev-зависимостей)::

    python tests/run_all.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.helpers import run_module  # noqa: E402

MODULES = [
    "tests.test_hand_eval",
    "tests.test_pot",
    "tests.test_deck_provider",
    "tests.test_game_flow",
    "tests.test_ws_client",
    "tests.test_simulation",
    # прогоны с живым сервисным слоем идут последними: они меняют глобальные
    # настройки (ускоряют таймеры), чтобы не ждать реального времени
    "tests.test_integration_table",
    "tests.test_bot_end_to_end",
]


def main() -> int:
    total_failures = 0
    for name in MODULES:
        print(f"\n{name}")
        module = importlib.import_module(name)
        total_failures += run_module(module)
    print()
    if total_failures:
        print(f"ПРОВАЛЕНО тестов: {total_failures}")
    else:
        print("Все тесты прошли.")
    return 1 if total_failures else 0


if __name__ == "__main__":
    sys.exit(main())
