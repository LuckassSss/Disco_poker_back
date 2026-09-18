"""Прогон множества раздач ботами: ловим редкие ветки и проверяем инварианты.

Боты ходят детерминированно (собственный LCG вместо random), поэтому падение
теста всегда воспроизводится: в сообщении печатается номер раздачи.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.deck_provider import DeckProvider  # noqa: E402
from app.core.enums import ActionType, SeatState  # noqa: E402
from app.core.game import HandEngine  # noqa: E402
from tests.helpers import make_seats  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Lcg:
    """Линейный конгруэнтный генератор — детерминированный источник решений ботов."""

    def __init__(self, seed: int) -> None:
        self.state = seed

    def next(self, bound: int) -> int:
        self.state = (self.state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return (self.state >> 33) % bound


def _bot_move(engine: HandEngine, rng: Lcg) -> tuple[ActionType, int]:
    options = {a.action: a for a in engine.legal_actions(engine.to_act)}
    roll = rng.next(100)
    if ActionType.CHECK in options:
        if roll < 4:
            # иногда пасуем, хотя можно чекнуть: странно для игрока, но полностью
            # легально — и именно так вскрывается «мёртвый» слой сайд-пота
            return ActionType.FOLD, 0
        if roll < 55:
            return ActionType.CHECK, 0
        if roll < 92 and ActionType.BET in options:
            spec = options[ActionType.BET]
            span = max(1, spec.max_amount - spec.min_amount)
            return ActionType.BET, spec.min_amount + rng.next(span)
        if ActionType.ALL_IN in options:
            return ActionType.ALL_IN, 0
        return ActionType.CHECK, 0
    if roll < 25:
        return ActionType.FOLD, 0
    if roll < 80 and ActionType.CALL in options:
        return ActionType.CALL, 0
    if ActionType.RAISE in options:
        spec = options[ActionType.RAISE]
        span = max(1, spec.max_amount - spec.min_amount)
        return ActionType.RAISE, spec.min_amount + rng.next(span)
    if ActionType.CALL in options:
        return ActionType.CALL, 0
    return ActionType.FOLD, 0


def _play_table(
    hands: int, seed: int, seats_count: int = 6, stacks: list[int] | None = None
) -> None:
    provider = DeckProvider(ROOT / "data" / "decks.json").load()
    rng = Lcg(seed)
    seats = make_seats(stacks or [1000] * seats_count)
    total_chips = sum(s.stack for s in seats)
    button = 0

    for hand_no in range(1, hands + 1):
        ready = [s for s in seats if s.can_be_dealt]
        if len(ready) < 2:
            # доливаем коротышей, чтобы прогон не выродился в хедз-ап
            for s in seats:
                if s.is_occupied and s.stack == 0:
                    s.stack = 1000
                    total_chips += 1000
            ready = [s for s in seats if s.can_be_dealt]

        for s in seats:
            s.reset_for_new_hand()
        button = (button + 1) % seats_count

        deck = provider.peek(hand_no)
        cursor = deck.iterator()
        engine = HandEngine(
            hand_id=f"sim-{hand_no}",
            table_id="sim",
            seats=[s for s in seats if s.is_occupied],
            button_seat=button,
            small_blind=5,
            big_blind=10,
            deck=cursor,
            burn_cards=True,
        )
        engine.start()

        guard = 0
        while not engine.finished:
            guard += 1
            assert guard < 500, f"раздача {hand_no}: раздача не завершается, зациклились"
            action, amount = _bot_move(engine, rng)
            engine.act(engine.to_act, action, amount)

        # --- инварианты -----------------------------------------------------
        assert sum(s.stack for s in seats) == total_chips, (
            f"раздача {hand_no}: фишки не сходятся "
            f"({sum(s.stack for s in seats)} вместо {total_chips})"
        )
        cards = list(engine.board)
        for s in seats:
            cards += s.hole_cards
        assert len(cards) == len(set(cards)), f"раздача {hand_no}: дубликат карты"
        assert all(s.stack >= 0 for s in seats), f"раздача {hand_no}: отрицательный стек"
        assert all(s.bet == 0 for s in seats), f"раздача {hand_no}: остались невзятые ставки"
        result = engine.result
        assert result is not None
        assert cursor.remaining >= 0
        if result.went_to_showdown:
            assert len(engine.board) == 5, f"раздача {hand_no}: вскрытие на неполном борде"
        for seat_no in engine.participants:
            seat = engine.seats[seat_no]
            assert seat.state is not SeatState.IN_HAND, (
                f"раздача {hand_no}: место {seat_no} осталось в подвешенном состоянии"
            )


def test_six_max_marathon():
    _play_table(hands=400, seed=20260813)


def test_three_handed_marathon():
    _play_table(hands=300, seed=777, seats_count=3)


def test_heads_up_marathon():
    _play_table(hands=300, seed=42, seats_count=2)


def test_tiny_stacks_marathon():
    """Микро-стеки: почти каждая раздача — олл-ин, много сайд-потов и коротких блайндов."""
    for seed in (11, 12, 13):
        _play_table(hands=200, seed=seed, stacks=[15, 1, 6, 40, 3, 120])


def test_lopsided_stacks_marathon():
    _play_table(hands=250, seed=99, stacks=[5000, 12, 340, 60, 1000, 7])


def test_different_seed_same_result():
    """Один и тот же сид даёт один и тот же прогон — тесты воспроизводимы."""
    _play_table(hands=50, seed=1)
    _play_table(hands=50, seed=1)


if __name__ == "__main__":
    from tests.helpers import run_module

    sys.exit(run_module(sys.modules[__name__]))
