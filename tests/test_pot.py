"""Тесты построения банков и распределения выигрыша."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.pot import build_pots, distribute  # noqa: E402


class Rank:
    """Заглушка силы руки: чем больше число, тем сильнее."""

    def __init__(self, value: int) -> None:
        self.value = value

    def __gt__(self, other: "Rank") -> bool:
        return self.value > other.value

    def __lt__(self, other: "Rank") -> bool:
        return self.value < other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Rank) and other.value == self.value

    def __hash__(self) -> int:
        return hash(self.value)


def test_single_pot_no_side():
    layout = build_pots({0: 100, 1: 100, 2: 100}, [0, 1, 2])
    assert len(layout.pots) == 1
    assert layout.pots[0].amount == 300
    assert layout.pots[0].eligible_seats == frozenset({0, 1, 2})


def test_side_pot_with_short_stack():
    """Игрок 0 в олл-ине на 50, двое других доигрывают до 200."""
    layout = build_pots({0: 50, 1: 200, 2: 200}, [0, 1, 2])
    assert len(layout.pots) == 2
    main, side = layout.pots
    assert main.amount == 150  # 50 x 3
    assert main.eligible_seats == frozenset({0, 1, 2})
    assert side.amount == 300  # (200-50) x 2
    assert side.eligible_seats == frozenset({1, 2})
    assert layout.total == 450


def test_uncalled_bet_is_returned():
    """Никто не смог заколлировать — излишек возвращается, а не идёт в банк."""
    layout = build_pots({0: 500, 1: 120}, [0, 1])
    assert layout.uncalled_returns == {0: 380}
    assert layout.total == 240


def test_folded_player_chips_stay_in_pot():
    """Сбросивший карты вложил фишки — они остаются в банке, но он на них не претендует."""
    layout = build_pots({0: 100, 1: 100, 2: 40}, [0, 1])
    assert layout.total == 240
    assert all(2 not in pot.eligible_seats for pot in layout.pots)


def test_three_way_all_in_ladder():
    layout = build_pots({0: 30, 1: 80, 2: 200, 3: 200}, [0, 1, 2, 3])
    amounts = [p.amount for p in layout.pots]
    assert amounts == [120, 150, 240]
    assert layout.pots[0].eligible_seats == frozenset({0, 1, 2, 3})
    assert layout.pots[1].eligible_seats == frozenset({1, 2, 3})
    assert layout.pots[2].eligible_seats == frozenset({2, 3})
    assert layout.total == 510 == 30 + 80 + 200 + 200


def test_distribution_split_with_odd_chip():
    """Нечётная фишка достаётся первому слева от баттона."""
    layout = build_pots({0: 51, 1: 51}, [0, 1])
    awards = distribute(layout, {0: Rank(5), 1: Rank(5)}, odd_chip_order=[1, 0])
    by_seat = {a.seat_no: a.amount for a in awards}
    assert sum(by_seat.values()) == 102
    assert by_seat[1] == 51 and by_seat[0] == 51


def test_distribution_odd_chip_goes_left_of_button():
    layout = build_pots({0: 5, 1: 5, 2: 5}, [0, 1])
    # банк 15, победители 0 и 1 → 8 и 7
    awards = distribute(layout, {0: Rank(9), 1: Rank(9)}, odd_chip_order=[1, 2, 0])
    by_seat = {a.seat_no: a.amount for a in awards}
    assert sum(by_seat.values()) == 15
    assert by_seat[1] == 8, "первый слева от баттона получает лишнюю фишку"
    assert by_seat[0] == 7


def test_short_all_in_wins_only_main_pot():
    layout = build_pots({0: 50, 1: 200, 2: 200}, [0, 1, 2])
    awards = distribute(
        layout,
        {0: Rank(9), 1: Rank(5), 2: Rank(3)},  # коротышка с лучшей рукой
        odd_chip_order=[0, 1, 2],
    )
    by_seat: dict[int, int] = {}
    for a in awards:
        by_seat[a.seat_no] = by_seat.get(a.seat_no, 0) + a.amount
    assert by_seat[0] == 150, "коротышка забирает только основной банк"
    assert by_seat[1] == 300, "сайд-пот уходит лучшему из оставшихся"
    assert 2 not in by_seat


def test_dead_layer_is_not_lost():
    """Слой, который сложили только спасовавшие, прирастает к банку, а не исчезает."""
    layout = build_pots({0: 5, 1: 1, 2: 5}, [1])
    assert layout.total + sum(layout.uncalled_returns.values()) == 11
    awards = distribute(layout, {1: Rank(1)}, odd_chip_order=[1, 2, 0])
    assert sum(a.amount for a in awards) == layout.total


def test_dead_side_pot_between_two_all_ins():
    """Два коротких олл-ина дошли до вскрытия, крупные вкладчики спасовали."""
    contributions = {0: 3, 1: 5, 2: 33, 3: 33}
    layout = build_pots(contributions, [0, 1])
    awards = distribute(layout, {0: Rank(2), 1: Rank(9)}, odd_chip_order=[1, 2, 3, 0])
    paid = sum(a.amount for a in awards) + sum(layout.uncalled_returns.values())
    assert paid == sum(contributions.values()) == 74


def test_everything_is_distributed():
    """Инвариант: сколько вложили — столько и раздали, ни фишкой больше."""
    contributions = {0: 37, 1: 91, 2: 91, 3: 12}
    layout = build_pots(contributions, [0, 1, 2, 3])
    awards = distribute(
        layout, {0: Rank(4), 1: Rank(8), 2: Rank(8), 3: Rank(1)}, odd_chip_order=[1, 2, 3, 0]
    )
    paid_out = sum(a.amount for a in awards) + sum(layout.uncalled_returns.values())
    assert paid_out == sum(contributions.values())


if __name__ == "__main__":
    from tests.helpers import run_module

    sys.exit(run_module(sys.modules[__name__]))
