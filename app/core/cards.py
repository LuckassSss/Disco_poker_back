"""Представление карт.

Карта кодируется строкой из двух символов: ранг + масть, например ``As``, ``Td``, ``2c``.
Такой формат читаем в логах и удобен для Unity-клиента (парсится в один enum).

Ранги: 2 3 4 5 6 7 8 9 T J Q K A
Масти:  c (clubs/трефы), d (diamonds/бубны), h (hearts/червы), s (spades/пики)
"""
from __future__ import annotations

from typing import Iterable

RANKS = "23456789TJQKA"
SUITS = "cdhs"

RANK_VALUES: dict[str, int] = {r: i + 2 for i, r in enumerate(RANKS)}  # 2..14
VALUE_RANKS: dict[int, str] = {v: r for r, v in RANK_VALUES.items()}

FULL_DECK: tuple[str, ...] = tuple(f"{r}{s}" for r in RANKS for s in SUITS)

assert len(FULL_DECK) == 52


def rank_of(card: str) -> int:
    """Числовой ранг карты (2..14)."""
    return RANK_VALUES[card[0]]


def suit_of(card: str) -> str:
    return card[1]


def is_valid_card(card: str) -> bool:
    return (
        isinstance(card, str)
        and len(card) == 2
        and card[0] in RANK_VALUES
        and card[1] in SUITS
    )


def validate_deck(cards: Iterable[str]) -> list[str]:
    """Проверяет, что перед нами корректная перестановка полной колоды.

    Возвращает список карт. Бросает ValueError при любой аномалии — это защита
    от битого ``decks.json``, из-за которого раздача поехала бы молча.
    """
    deck = list(cards)
    if len(deck) != 52:
        raise ValueError(f"В колоде должно быть 52 карты, получено {len(deck)}")
    bad = [c for c in deck if not is_valid_card(c)]
    if bad:
        raise ValueError(f"Некорректные карты в колоде: {bad[:5]}")
    if len(set(deck)) != 52:
        raise ValueError("В колоде есть дубликаты карт")
    return deck


def card_to_int(card: str) -> int:
    """Компактный код карты 0..51 — для бинарного протокола, если понадобится."""
    return (RANK_VALUES[card[0]] - 2) * 4 + SUITS.index(card[1])


def int_to_card(code: int) -> str:
    if not 0 <= code < 52:
        raise ValueError(f"Код карты вне диапазона: {code}")
    return f"{VALUE_RANKS[code // 4 + 2]}{SUITS[code % 4]}"
