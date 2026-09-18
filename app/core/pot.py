"""Построение основного банка и сайд-потов + распределение выигрыша.

Все суммы — целые числа фишек. Дробей нет принципиально: нечётные фишки
при сплите отдаются по правилу «первый слева от баттона».
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence


@dataclass(slots=True)
class Pot:
    amount: int
    #: места, которые имеют право претендовать на этот банк
    eligible_seats: frozenset[int]
    #: 0 — основной банк, дальше сайд-поты по возрастанию
    index: int = 0


@dataclass(slots=True)
class PotAward:
    seat_no: int
    amount: int
    pot_index: int


@dataclass(slots=True)
class PotLayout:
    pots: list[Pot] = field(default_factory=list)
    #: неколированная часть ставки, возвращаемая игроку до вскрытия
    uncalled_returns: dict[int, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(p.amount for p in self.pots)


def build_pots(
    contributions: Mapping[int, int],
    active_seats: Sequence[int],
) -> PotLayout:
    """Раскладывает вклады игроков на основной банк и сайд-поты.

    :param contributions: место -> сколько всего фишек вложено за раздачу
                          (включая вклады сбросивших карты).
    :param active_seats: места, не сбросившие карты (претендуют на банк).
    """
    layout = PotLayout()
    contrib = {s: c for s, c in contributions.items() if c > 0}
    if not contrib:
        return layout

    active = set(active_seats)

    # --- возврат неколированной ставки -------------------------------------
    # Если один игрок вложил больше, чем кто-либо смог заколлировать, излишек
    # физически не участвует в игре и возвращается ему до вскрытия.
    ordered = sorted(contrib.values(), reverse=True)
    if len(ordered) >= 2 and ordered[0] > ordered[1]:
        top_seat = max(contrib, key=lambda s: contrib[s])
        excess = ordered[0] - ordered[1]
        contrib[top_seat] -= excess
        layout.uncalled_returns[top_seat] = excess
    elif len(ordered) == 1:
        # все, кроме одного, ничего не вложили — забирает всё обратно
        top_seat = next(iter(contrib))
        layout.uncalled_returns[top_seat] = contrib[top_seat]
        return layout

    # --- слои по уровням вклада --------------------------------------------
    levels = sorted({c for c in contrib.values() if c > 0})
    previous = 0
    #: «мёртвые» фишки из слоёв, все вкладчики которых сбросили карты
    carry = 0
    for level in levels:
        step = level - previous
        previous = level
        if step <= 0:
            continue
        amount = 0
        contributors: set[int] = set()
        for seat, total in contrib.items():
            if total >= level:
                amount += step
                contributors.add(seat)
        if amount <= 0:
            continue
        eligible = frozenset(contributors & active)
        if not eligible:
            # весь слой сложили спасовавшие игроки — фишки не исчезают,
            # а прирастают к ближайшему банку, у которого претенденты есть
            carry += amount
            continue
        amount += carry
        carry = 0
        if layout.pots and layout.pots[-1].eligible_seats == eligible:
            # слои с одинаковым составом претендентов схлопываем в один банк
            layout.pots[-1].amount += amount
        else:
            layout.pots.append(Pot(amount=amount, eligible_seats=eligible))

    if carry:
        if layout.pots:
            layout.pots[-1].amount += carry
        else:
            layout.pots.append(Pot(amount=carry, eligible_seats=frozenset(active)))

    for i, pot in enumerate(layout.pots):
        pot.index = i

    # инвариант: ни одна фишка не потерялась по дороге
    accounted = layout.total + sum(layout.uncalled_returns.values())
    expected = sum(contributions.values())
    if accounted != expected:  # pragma: no cover — защита от регрессии
        raise ValueError(
            f"Банки не сходятся: разложено {accounted}, вложено {expected} "
            f"(вклады={dict(contributions)}, активные={sorted(active)})"
        )
    return layout


def distribute(
    layout: PotLayout,
    ranks: Mapping[int, object],
    odd_chip_order: Sequence[int],
) -> list[PotAward]:
    """Распределяет банки между победителями.

    :param ranks: место -> сравнимый объект силы руки (``HandRank``).
                  Места, отсутствующие в словаре, считаются сбросившими.
    :param odd_chip_order: порядок мест начиная с первого слева от баттона —
                           по нему раздаются нечётные фишки при сплите.
    """
    awards: list[PotAward] = []
    for pot in layout.pots:
        claimants = [s for s in pot.eligible_seats if s in ranks]
        if not claimants:
            # теоретически невозможно: банк без претендентов
            continue
        best = max(ranks[s] for s in claimants)  # type: ignore[type-var]
        winners = [s for s in claimants if ranks[s] == best]
        share, remainder = divmod(pot.amount, len(winners))
        winners_sorted = sorted(
            winners,
            key=lambda s: odd_chip_order.index(s) if s in odd_chip_order else 99,
        )
        for i, seat in enumerate(winners_sorted):
            amount = share + (1 if i < remainder else 0)
            if amount:
                awards.append(PotAward(seat_no=seat, amount=amount, pot_index=pot.index))
    return awards
