#!/usr/bin/env python3
"""Генератор файла заранее замешанных колод ``data/decks.json``.

Запускается ОДИН РАЗ разработчиком, а не сервером. В рантайме сервер только
читает готовый файл — никакой генерации случайных чисел во время игры нет.

Перемешивание детерминированное и воспроизводимое: Fisher–Yates, где источник
«случайности» — поток байт SHA-256 от строки ``{seed_prefix}:{deck_index}:{counter}``.
Это значит, что любой человек может перегенерировать файл и байт в байт сверить
результат — полезно и для тестов, и для аудита честности.

Использование::

    python scripts/generate_decks.py                 # 10 колод, сид по умолчанию
    python scripts/generate_decks.py --count 100     # когда захотим на порядок больше
    python scripts/generate_decks.py --seed my-seed --out data/decks.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.cards import FULL_DECK, validate_deck  # noqa: E402

DEFAULT_SEED = "neon-poker-v1"
DEFAULT_COUNT = 10
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "decks.json"


class _HashStream:
    """Бесконечный детерминированный поток байт из SHA-256."""

    def __init__(self, seed: str) -> None:
        self._seed = seed.encode()
        self._counter = 0
        self._buffer = b""

    def _refill(self) -> None:
        self._buffer += hashlib.sha256(
            self._seed + b":" + str(self._counter).encode()
        ).digest()
        self._counter += 1

    def randbelow(self, n: int) -> int:
        """Равномерное целое из [0, n) без смещения (rejection sampling)."""
        if n <= 1:
            return 0
        # берём 8 байт и отбрасываем значения из «хвоста», который не делится на n
        limit = (1 << 64) // n * n
        while True:
            while len(self._buffer) < 8:
                self._refill()
            chunk, self._buffer = self._buffer[:8], self._buffer[8:]
            value = int.from_bytes(chunk, "big")
            if value < limit:
                return value % n


def shuffle_deck(seed_prefix: str, deck_index: int) -> list[str]:
    """Одна перетасовка Fisher–Yates от детерминированного потока."""
    stream = _HashStream(f"{seed_prefix}:{deck_index}")
    deck = list(FULL_DECK)
    for i in range(len(deck) - 1, 0, -1):
        j = stream.randbelow(i + 1)
        deck[i], deck[j] = deck[j], deck[i]
    return validate_deck(deck)


def build(seed_prefix: str, count: int) -> dict:
    decks = []
    for i in range(count):
        cards = shuffle_deck(seed_prefix, i)
        decks.append(
            {
                "id": i,
                "sha256": hashlib.sha256(",".join(cards).encode()).hexdigest(),
                "cards": cards,
            }
        )
    return {
        "version": 1,
        "algorithm": "sha256-fisher-yates",
        "seed_prefix": seed_prefix,
        "count": count,
        "note": (
            "Сгенерировано scripts/generate_decks.py. Файл детерминирован: "
            "тот же seed_prefix и count дают тот же результат."
        ),
        "decks": decks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    payload = build(args.seed, args.count)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Записано {payload['count']} колод в {args.out}")
    for deck in payload["decks"]:
        print(f"  #{deck['id']:>3}  {deck['sha256'][:16]}…  {' '.join(deck['cards'][:8])} …")


if __name__ == "__main__":
    main()
