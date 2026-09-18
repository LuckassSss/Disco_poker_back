"""Вспомогательные штуки для тестов: подставная колода и заготовка стола."""
from __future__ import annotations

from app.core.cards import FULL_DECK
from app.core.deck_provider import DealtDeck, DeckCursor
from app.core.enums import SeatState
from app.core.game import HandEngine
from app.core.models import Seat


def stacked_deck(*cards: str, deck_id: int = 0, sequence: int = 1) -> DeckCursor:
    """Колода с заданным началом: остальные карты досыпаются из полной колоды.

    Позволяет писать тесты вида «раздай вот эти карманные и вот такой флоп».
    """
    head = list(cards)
    rest = [c for c in FULL_DECK if c not in head]
    return DeckCursor(
        DealtDeck(deck_id=deck_id, sequence=sequence, cards=tuple(head + rest), sha256="test")
    )


def make_seats(stacks: list[int], nicknames: list[str] | None = None) -> list[Seat]:
    seats = []
    for i, stack in enumerate(stacks):
        seat = Seat(seat_no=i)
        if stack > 0:
            seat.player_id = f"p{i}"
            seat.nickname = (nicknames or [])[i] if nicknames else f"Игрок{i}"
            seat.stack = stack
            seat.state = SeatState.WAITING
            seat.connected = True
        seats.append(seat)
    return seats


def make_engine(
    stacks: list[int],
    deck: DeckCursor,
    button: int = 0,
    sb: int = 5,
    bb: int = 10,
    burn: bool = False,
) -> tuple[HandEngine, list[Seat]]:
    seats = make_seats(stacks)
    engine = HandEngine(
        hand_id="test-hand",
        table_id="test-table",
        seats=[s for s in seats if s.is_occupied],
        button_seat=button,
        small_blind=sb,
        big_blind=bb,
        deck=deck,
        burn_cards=burn,
    )
    return engine, seats


def run_module(module) -> int:
    """Мини-раннер: выполняет все test_* из модуля без pytest.

    Нужен, чтобы прогнать логику там, где pytest не установлен;
    обычный ``pytest tests/`` работает ровно так же.
    """
    failures = 0
    names = [n for n in dir(module) if n.startswith("test_")]
    for name in sorted(names):
        fn = getattr(module, name)
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    return failures
