"""Перечисления игрового ядра."""
from __future__ import annotations

from enum import Enum


class Street(str, Enum):
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"
    SHOWDOWN = "showdown"


class SeatState(str, Enum):
    EMPTY = "empty"
    # Сидит за столом, но не участвует в раздачах (сел между раздачами / сам вышел в sit-out)
    SITTING_OUT = "sitting_out"
    # Ждёт начала следующей раздачи
    WAITING = "waiting"
    # Участвует в текущей раздаче
    IN_HAND = "in_hand"
    FOLDED = "folded"
    ALL_IN = "all_in"


class ActionType(str, Enum):
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    BET = "bet"
    RAISE = "raise"
    ALL_IN = "all_in"


class TableState(str, Enum):
    WAITING_FOR_PLAYERS = "waiting_for_players"
    HAND_IN_PROGRESS = "hand_in_progress"
    PAUSED = "paused"


class HandCategory(int, Enum):
    HIGH_CARD = 1
    PAIR = 2
    TWO_PAIR = 3
    TRIPS = 4
    STRAIGHT = 5
    FLUSH = 6
    FULL_HOUSE = 7
    QUADS = 8
    STRAIGHT_FLUSH = 9

    @property
    def title(self) -> str:
        return _CATEGORY_TITLES[self]


_CATEGORY_TITLES = {
    HandCategory.HIGH_CARD: "Старшая карта",
    HandCategory.PAIR: "Пара",
    HandCategory.TWO_PAIR: "Две пары",
    HandCategory.TRIPS: "Тройка",
    HandCategory.STRAIGHT: "Стрит",
    HandCategory.FLUSH: "Флеш",
    HandCategory.FULL_HOUSE: "Фулл-хаус",
    HandCategory.QUADS: "Каре",
    HandCategory.STRAIGHT_FLUSH: "Стрит-флеш",
}
