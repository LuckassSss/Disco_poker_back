"""Оценка покерной комбинации из 5–7 карт (Texas Hold'em).

Результат — ``HandRank`` с полем ``key``: кортеж, который можно напрямую
сравнивать оператором ``>``/``==``. Равные ``key`` означают абсолютно равные
руки (сплит-пот).

Алгоритм прямой, без таблиц предпросчёта: 7 карт — это максимум 21 комбинация,
но мы даже их не перебираем, а собираем лучшую пятёрку аналитически.
Скорость: ~5 мкс на руку, для 5-max стола этого с огромным запасом хватает.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from app.core.cards import RANK_VALUES, VALUE_RANKS, rank_of, suit_of
from app.core.enums import HandCategory

# Порядок «колеса» A-2-3-4-5: туз работает как 1
_WHEEL = (14, 5, 4, 3, 2)


@dataclass(frozen=True, slots=True, eq=False)
class HandRank:
    """Сила руки.

    Сравнение идёт ИСКЛЮЧИТЕЛЬНО по ``key``. Это принципиально: ``best_five``
    у двух игроков с одинаковой по силе рукой почти всегда различается мастями
    (например, у обоих стрит до валета, но один играет свою десятку, другой —
    свою). Если бы равенство учитывало ``best_five``, сплит-поты бы не работали
    и банк целиком уходил бы одному игроку. Поэтому ``eq=False`` и все операторы
    сравнения написаны руками.
    """

    category: HandCategory
    key: tuple[int, ...]
    best_five: tuple[str, ...]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, HandRank) and self.key == other.key

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(self.key)

    def __lt__(self, other: "HandRank") -> bool:
        return self.key < other.key

    def __gt__(self, other: "HandRank") -> bool:
        return self.key > other.key

    def __le__(self, other: "HandRank") -> bool:
        return self.key <= other.key

    def __ge__(self, other: "HandRank") -> bool:
        return self.key >= other.key

    @property
    def title(self) -> str:
        return self.category.title

    def describe(self) -> str:
        """Человекочитаемое описание — уходит в лог раздачи и на клиент."""
        cat = self.category
        v = self.key[1:]
        n = VALUE_RANKS
        if cat is HandCategory.STRAIGHT_FLUSH:
            return "Роял-флеш" if v[0] == 14 else f"Стрит-флеш до {n[v[0]]}"
        if cat is HandCategory.QUADS:
            return f"Каре {n[v[0]]}"
        if cat is HandCategory.FULL_HOUSE:
            return f"Фулл-хаус {n[v[0]]} на {n[v[1]]}"
        if cat is HandCategory.FLUSH:
            return f"Флеш до {n[v[0]]}"
        if cat is HandCategory.STRAIGHT:
            return f"Стрит до {n[v[0]]}"
        if cat is HandCategory.TRIPS:
            return f"Сет {n[v[0]]}"
        if cat is HandCategory.TWO_PAIR:
            return f"Две пары: {n[v[0]]} и {n[v[1]]}"
        if cat is HandCategory.PAIR:
            return f"Пара {n[v[0]]}"
        return f"Старшая карта {n[v[0]]}"


def _straight_high(values: Sequence[int]) -> int | None:
    """Возвращает старшую карту стрита из набора уникальных рангов или None."""
    uniq = sorted(set(values), reverse=True)
    if 14 in uniq:
        uniq.append(1)  # туз снизу для «колеса»
    run = 1
    for i in range(1, len(uniq)):
        if uniq[i] == uniq[i - 1] - 1:
            run += 1
            if run >= 5:
                return uniq[i] + 4
        else:
            run = 1
    return None


def _cards_for_straight(cards: Sequence[str], high: int) -> tuple[str, ...]:
    wanted = [high - i for i in range(5)]
    if high == 5:
        wanted = list(_WHEEL)
    picked: list[str] = []
    for value in wanted:
        for c in cards:
            if rank_of(c) == value and c not in picked:
                picked.append(c)
                break
    return tuple(picked)


def evaluate(cards: Sequence[str]) -> HandRank:
    """Лучшая пятикарточная комбинация из 5, 6 или 7 карт."""
    if not 5 <= len(cards) <= 7:
        raise ValueError(f"Ожидалось 5–7 карт, получено {len(cards)}")

    values = [rank_of(c) for c in cards]
    suits = [suit_of(c) for c in cards]

    # ---- флеш / стрит-флеш -------------------------------------------------
    suit_counts = Counter(suits)
    flush_suit = next((s for s, n in suit_counts.items() if n >= 5), None)
    if flush_suit is not None:
        flush_cards = [c for c in cards if suit_of(c) == flush_suit]
        flush_values = [rank_of(c) for c in flush_cards]
        sf_high = _straight_high(flush_values)
        if sf_high is not None:
            return HandRank(
                HandCategory.STRAIGHT_FLUSH,
                (HandCategory.STRAIGHT_FLUSH.value, sf_high),
                _cards_for_straight(flush_cards, sf_high),
            )

    # ---- группировка по рангам ---------------------------------------------
    rank_counts = Counter(values)
    # сортируем: сначала по количеству, затем по рангу — оба по убыванию
    groups = sorted(rank_counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)

    def pick(value: int, count: int, exclude: set[str] | None = None) -> list[str]:
        exclude = exclude or set()
        out = [c for c in cards if rank_of(c) == value and c not in exclude]
        return out[:count]

    top_value, top_count = groups[0]

    if top_count == 4:
        quad = pick(top_value, 4)
        kicker = max(v for v in values if v != top_value)
        best = tuple(quad + pick(kicker, 1, set(quad)))
        return HandRank(HandCategory.QUADS, (HandCategory.QUADS.value, top_value, kicker), best)

    if top_count == 3:
        # ищем вторую тройку или пару под фулл-хаус
        pair_value = next((v for v, n in groups[1:] if n >= 2), None)
        if pair_value is not None:
            trips = pick(top_value, 3)
            pair = pick(pair_value, 2, set(trips))
            return HandRank(
                HandCategory.FULL_HOUSE,
                (HandCategory.FULL_HOUSE.value, top_value, pair_value),
                tuple(trips + pair),
            )

    if flush_suit is not None:
        flush_cards = sorted(
            (c for c in cards if suit_of(c) == flush_suit), key=rank_of, reverse=True
        )[:5]
        fv = tuple(rank_of(c) for c in flush_cards)
        return HandRank(HandCategory.FLUSH, (HandCategory.FLUSH.value, *fv), tuple(flush_cards))

    straight = _straight_high(values)
    if straight is not None:
        return HandRank(
            HandCategory.STRAIGHT,
            (HandCategory.STRAIGHT.value, straight),
            _cards_for_straight(sorted(cards, key=rank_of, reverse=True), straight),
        )

    if top_count == 3:
        trips = pick(top_value, 3)
        kickers = sorted((v for v in values if v != top_value), reverse=True)[:2]
        used = set(trips)
        extra: list[str] = []
        for k in kickers:
            extra += pick(k, 1, used)
            used |= set(extra)
        return HandRank(
            HandCategory.TRIPS,
            (HandCategory.TRIPS.value, top_value, *kickers),
            tuple(trips + extra),
        )

    pairs = [v for v, n in groups if n == 2]
    if len(pairs) >= 2:
        hi, lo = pairs[0], pairs[1]
        cards_hi = pick(hi, 2)
        cards_lo = pick(lo, 2)
        used = set(cards_hi + cards_lo)
        kicker = max(v for v in values if v not in (hi, lo))
        best = tuple(cards_hi + cards_lo + pick(kicker, 1, used))
        return HandRank(
            HandCategory.TWO_PAIR, (HandCategory.TWO_PAIR.value, hi, lo, kicker), best
        )

    if len(pairs) == 1:
        p = pairs[0]
        pair_cards = pick(p, 2)
        kickers = sorted((v for v in values if v != p), reverse=True)[:3]
        used = set(pair_cards)
        extra = []
        for k in kickers:
            got = pick(k, 1, used)
            extra += got
            used |= set(got)
        return HandRank(
            HandCategory.PAIR,
            (HandCategory.PAIR.value, p, *kickers),
            tuple(pair_cards + extra),
        )

    top5 = sorted(cards, key=rank_of, reverse=True)[:5]
    return HandRank(
        HandCategory.HIGH_CARD,
        (HandCategory.HIGH_CARD.value, *[rank_of(c) for c in top5]),
        tuple(top5),
    )


def best_hand(hole: Sequence[str], board: Sequence[str]) -> HandRank:
    """Комбинация игрока: 2 карманные + общие карты стола."""
    return evaluate(list(hole) + list(board))


__all__ = ["HandRank", "evaluate", "best_hand", "RANK_VALUES"]
