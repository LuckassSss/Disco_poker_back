"""Машина состояний одной раздачи No-Limit Texas Hold'em.

Класс синхронный и не знает ни про сеть, ни про asyncio: на вход — места и
курсор колоды, на выход — список событий, которые сетевой слой рассылает
клиентам. Благодаря этому всю игровую логику можно прогонять в тестах
без сервера.

Ответственность разделена так:
  * ``HandEngine`` — правила: очередь хода, легальность ставок, улицы, вскрытие;
  * ``Table`` (см. ``services/table.py``) — таймеры, сокеты, посадка игроков.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from app.core.deck_provider import DeckCursor
from app.core.enums import ActionType, SeatState, Street
from app.core.hand_eval import HandRank, best_hand
from app.core.models import Seat
from app.core.pot import PotAward, build_pots, distribute

log = logging.getLogger(__name__)


class InvalidAction(Exception):
    """Клиент прислал ход, недопустимый в текущем состоянии."""


@dataclass(slots=True)
class Event:
    type: str
    data: dict
    #: если задано — событие видит только этот игрок (карманные карты)
    private_to: str | None = None


@dataclass(slots=True)
class LegalAction:
    action: ActionType
    #: для call/bet/raise — минимальная и максимальная сумма ставки «до»
    min_amount: int = 0
    max_amount: int = 0

    def as_dict(self) -> dict:
        d: dict = {"action": self.action.value}
        if self.action in (ActionType.CALL, ActionType.BET, ActionType.RAISE, ActionType.ALL_IN):
            d["min_amount"] = self.min_amount
            d["max_amount"] = self.max_amount
        return d


@dataclass(slots=True)
class HandResult:
    hand_id: str
    board: list[str]
    awards: list[PotAward]
    #: место -> (карты, описание комбинации); только для дошедших до вскрытия
    showdown: dict[int, tuple[list[str], str]] = field(default_factory=dict)
    returns: dict[int, int] = field(default_factory=dict)
    total_pot: int = 0
    went_to_showdown: bool = False


STREET_ORDER = (Street.PREFLOP, Street.FLOP, Street.TURN, Street.RIVER)


class HandEngine:
    """Одна раздача от постановки блайндов до распределения банка."""

    def __init__(
        self,
        *,
        hand_id: str,
        table_id: str,
        seats: Sequence[Seat],
        button_seat: int,
        small_blind: int,
        big_blind: int,
        deck: DeckCursor,
        burn_cards: bool = True,
    ) -> None:
        self.hand_id = hand_id
        self.table_id = table_id
        self.seats: dict[int, Seat] = {s.seat_no: s for s in seats}
        self.seat_order: list[int] = sorted(self.seats)
        self.button_seat = button_seat
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.deck = deck
        self.burn_cards = burn_cards

        self.street: Street = Street.PREFLOP
        self.board: list[str] = []
        self.events: list[Event] = []

        self.current_bet: int = 0
        self.min_raise: int = big_blind
        self.to_act: int | None = None
        self.last_aggressor: int | None = None
        #: места, сходившие после последнего полноценного рейза
        self.acted: set[int] = set()
        self.finished: bool = False
        self.result: HandResult | None = None
        #: участники раздачи в порядке рассадки
        self.participants: list[int] = [
            s.seat_no for s in sorted(seats, key=lambda x: x.seat_no) if s.can_be_dealt
        ]
        self._pot_snapshot: int = 0

    # ------------------------------------------------------------------ utils
    def _emit(self, type_: str, data: dict, private_to: str | None = None) -> None:
        self.events.append(Event(type=type_, data=data, private_to=private_to))

    def drain_events(self) -> list[Event]:
        out, self.events = self.events, []
        return out

    def _seats_from(self, start_seat: int, include_start: bool = False) -> Iterable[Seat]:
        """Обход мест по часовой стрелке начиная с ``start_seat``."""
        order = self.seat_order
        if start_seat not in order:
            start_index = 0
        else:
            start_index = order.index(start_seat)
        n = len(order)
        rng = range(0, n) if include_start else range(1, n + 1)
        for offset in rng:
            yield self.seats[order[(start_index + offset) % n]]

    def _next_occupied_in_hand(self, start_seat: int) -> int | None:
        for seat in self._seats_from(start_seat):
            if seat.seat_no in self.participants and seat.state != SeatState.FOLDED:
                return seat.seat_no
        return None

    @property
    def active_seats(self) -> list[int]:
        """Не сбросившие карты."""
        return [
            s for s in self.participants
            if self.seats[s].state in (SeatState.IN_HAND, SeatState.ALL_IN)
        ]

    @property
    def pot_total(self) -> int:
        return sum(self.seats[s].committed for s in self.participants)

    # ------------------------------------------------------------------ start
    def start(self) -> None:
        if len(self.participants) < 2:
            raise InvalidAction("Для раздачи нужно минимум два игрока")

        for seat_no in self.participants:
            seat = self.seats[seat_no]
            seat.hole_cards = []
            seat.bet = 0
            seat.committed = 0
            seat.all_in = False
            seat.state = SeatState.IN_HAND

        heads_up = len(self.participants) == 2
        if heads_up:
            sb_seat = self.button_seat
            bb_seat = self._next_participant(self.button_seat)
        else:
            sb_seat = self._next_participant(self.button_seat)
            bb_seat = self._next_participant(sb_seat)

        self._emit(
            "hand_started",
            {
                "hand_id": self.hand_id,
                "table_id": self.table_id,
                "button_seat": self.button_seat,
                "small_blind_seat": sb_seat,
                "big_blind_seat": bb_seat,
                "small_blind": self.small_blind,
                "big_blind": self.big_blind,
                "deck_id": self.deck.deck_id,
                "deck_sequence": self.deck.sequence,
                "players": [
                    {
                        "seat_no": s,
                        "player_id": self.seats[s].player_id,
                        "nickname": self.seats[s].nickname,
                        "stack": self.seats[s].stack,
                    }
                    for s in self.participants
                ],
            },
        )

        self._post_blind(sb_seat, self.small_blind, "small_blind")
        self._post_blind(bb_seat, self.big_blind, "big_blind")
        self.current_bet = self.big_blind
        self.min_raise = self.big_blind
        self.last_aggressor = bb_seat

        self._deal_hole_cards(sb_seat)

        # первый ход: в HU — баттон (он же SB), иначе — следующий за BB.
        # Начальную точку берём как ориентир, а дальше ищем первого, кто вообще
        # может ходить: блайнд мог уйти в олл-ин прямо на постановке.
        first = self.button_seat if heads_up else self._next_participant(bb_seat)
        self.to_act = self._first_actor_from(first)
        if self.to_act is not None:
            self._request_action()
        else:
            self._advance_street()

    def _first_actor_from(self, start_seat: int) -> int | None:
        for seat in self._seats_from(start_seat, include_start=True):
            if self._seat_needs_action(seat):
                return seat.seat_no
        return None

    def _next_participant(self, from_seat: int) -> int:
        for seat in self._seats_from(from_seat):
            if seat.seat_no in self.participants:
                return seat.seat_no
        raise InvalidAction("Не найден следующий участник раздачи")

    def _post_blind(self, seat_no: int, amount: int, kind: str) -> None:
        seat = self.seats[seat_no]
        paid = min(amount, seat.stack)
        seat.stack -= paid
        seat.bet += paid
        seat.committed += paid
        if seat.stack == 0:
            seat.state = SeatState.ALL_IN
            seat.all_in = True
        self._emit(
            "blind_posted",
            {
                "seat_no": seat_no,
                "kind": kind,
                "amount": paid,
                "stack": seat.stack,
                "all_in": seat.stack == 0,
                "pot": self.pot_total,
            },
        )

    def _deal_hole_cards(self, first_seat: int) -> None:
        # раздаём по одной карте по кругу, как за живым столом
        order = [first_seat]
        cur = first_seat
        while len(order) < len(self.participants):
            cur = self._next_participant(cur)
            order.append(cur)
        for _ in range(2):
            for seat_no in order:
                self.seats[seat_no].hole_cards.extend(self.deck.draw(1))

        for seat_no in order:
            seat = self.seats[seat_no]
            self._emit(
                "hole_cards",
                {"seat_no": seat_no, "cards": list(seat.hole_cards)},
                private_to=seat.player_id,
            )
        self._emit(
            "cards_dealt",
            {"seats": order, "street": Street.PREFLOP.value},
        )

    # ----------------------------------------------------------- legal actions
    def _seat_needs_action(self, seat: Seat) -> bool:
        if seat.seat_no not in self.participants:
            return False
        if seat.state != SeatState.IN_HAND:
            return False
        if seat.stack <= 0:
            return False
        return seat.seat_no not in self.acted or seat.bet < self.current_bet

    def _active_can_act(self) -> bool:
        return any(self._seat_needs_action(self.seats[s]) for s in self.participants)

    def legal_actions(self, seat_no: int) -> list[LegalAction]:
        seat = self.seats.get(seat_no)
        if seat is None or self.to_act != seat_no or self.finished:
            return []
        actions: list[LegalAction] = []
        to_call = self.current_bet - seat.bet

        actions.append(LegalAction(ActionType.FOLD))

        if to_call <= 0:
            actions.append(LegalAction(ActionType.CHECK))
        else:
            call_amount = min(to_call, seat.stack)
            actions.append(
                LegalAction(ActionType.CALL, min_amount=call_amount, max_amount=call_amount)
            )

        # можно ли повышать? нельзя, если игрок уже ходил после последнего
        # полноценного рейза, а ставку подняли лишь неполным олл-ином
        may_raise = seat.seat_no not in self.acted or self.current_bet - seat.bet >= self.min_raise
        max_to = seat.bet + seat.stack  # ставка «до», если пойти олл-ин

        # больше эффективного стека соперников ставить бессмысленно: излишек
        # всё равно вернётся. Ограничиваем, чтобы клиент не рисовал мёртвый слайдер.
        opponents_max = max(
            (self.seats[s].bet + self.seats[s].stack for s in self.active_seats if s != seat_no),
            default=self.current_bet,
        )
        effective_max = min(max_to, max(self.current_bet, opponents_max))
        can_raise = may_raise and effective_max > self.current_bet

        if can_raise:
            if self.current_bet == 0:
                min_to = min(self.big_blind, effective_max)
                actions.append(
                    LegalAction(ActionType.BET, min_amount=min_to, max_amount=effective_max)
                )
            else:
                min_to = min(self.current_bet + self.min_raise, effective_max)
                actions.append(
                    LegalAction(ActionType.RAISE, min_amount=min_to, max_amount=effective_max)
                )
            actions.append(LegalAction(ActionType.ALL_IN, min_amount=max_to, max_amount=max_to))
        elif max_to <= self.current_bet:
            # олл-ин меньше суммы колла — это не повышение, а вынужденный колл,
            # он разрешён всегда, даже когда право на рейз потеряно
            actions.append(LegalAction(ActionType.ALL_IN, min_amount=max_to, max_amount=max_to))

        return actions

    def _request_action(self) -> None:
        assert self.to_act is not None
        seat = self.seats[self.to_act]
        self._emit(
            "action_required",
            {
                "seat_no": seat.seat_no,
                "player_id": seat.player_id,
                "hand_id": self.hand_id,
                "street": self.street.value,
                "to_call": max(0, self.current_bet - seat.bet),
                "current_bet": self.current_bet,
                "min_raise": self.min_raise,
                "pot": self.pot_total,
                "stack": seat.stack,
                "options": [a.as_dict() for a in self.legal_actions(seat.seat_no)],
            },
        )

    # ------------------------------------------------------------------ action
    def act(self, seat_no: int, action: ActionType, amount: int = 0) -> None:
        if self.finished:
            raise InvalidAction("Раздача уже завершена")
        if self.to_act != seat_no:
            raise InvalidAction("Сейчас не ваш ход")
        seat = self.seats[seat_no]
        legal = {a.action: a for a in self.legal_actions(seat_no)}
        if action not in legal:
            raise InvalidAction(f"Действие {action.value} недоступно")

        to_call = self.current_bet - seat.bet

        if action is ActionType.FOLD:
            seat.state = SeatState.FOLDED
            self.acted.add(seat_no)
            self._emit_action(seat, action, 0)

        elif action is ActionType.CHECK:
            self.acted.add(seat_no)
            self._emit_action(seat, action, 0)

        elif action is ActionType.CALL:
            paid = min(to_call, seat.stack)
            self._put_chips(seat, paid)
            self.acted.add(seat_no)
            self._emit_action(seat, action, paid)

        elif action in (ActionType.BET, ActionType.RAISE, ActionType.ALL_IN):
            spec = legal[action]
            target = spec.max_amount if action is ActionType.ALL_IN else int(amount)
            if action is not ActionType.ALL_IN:
                if target < spec.min_amount or target > spec.max_amount:
                    raise InvalidAction(
                        f"Ставка {target} вне допустимого диапазона "
                        f"[{spec.min_amount}, {spec.max_amount}]"
                    )
            paid = target - seat.bet
            self._put_chips(seat, paid)
            increment = seat.bet - self.current_bet
            full_raise = increment >= self.min_raise or self.current_bet == 0
            if seat.bet > self.current_bet:
                self.current_bet = seat.bet
                self.last_aggressor = seat_no
                if full_raise:
                    self.min_raise = max(increment, self.big_blind)
                    self.acted = {seat_no}  # действие открыто заново для всех
                else:
                    # неполный олл-ин не открывает круг торговли заново
                    self.acted.add(seat_no)
            else:
                self.acted.add(seat_no)
            self._emit_action(seat, action, paid)
        else:  # pragma: no cover
            raise InvalidAction(f"Неизвестное действие {action}")

        self._after_action()

    def _put_chips(self, seat: Seat, amount: int) -> None:
        amount = max(0, min(amount, seat.stack))
        seat.stack -= amount
        seat.bet += amount
        seat.committed += amount
        if seat.stack == 0:
            seat.state = SeatState.ALL_IN
            seat.all_in = True

    def _emit_action(self, seat: Seat, action: ActionType, amount: int) -> None:
        self._emit(
            "player_action",
            {
                "seat_no": seat.seat_no,
                "player_id": seat.player_id,
                "action": action.value,
                "amount": amount,
                "bet": seat.bet,
                "stack": seat.stack,
                "state": seat.state.value,
                "pot": self.pot_total,
                "street": self.street.value,
            },
        )

    def _after_action(self) -> None:
        if len(self.active_seats) <= 1:
            self._finish(went_to_showdown=False)
            return
        if self._active_can_act():
            nxt = self._next_to_act()
            if nxt is not None:
                self.to_act = nxt
                self._request_action()
                return
        self._advance_street()

    def _next_to_act(self) -> int | None:
        assert self.to_act is not None
        for seat in self._seats_from(self.to_act):
            if self._seat_needs_action(seat):
                return seat.seat_no
        return None

    # ------------------------------------------------------------------ streets
    def _collect_bets(self) -> None:
        for seat_no in self.participants:
            self.seats[seat_no].bet = 0
        self.current_bet = 0
        self.min_raise = self.big_blind
        self.acted = set()
        self.last_aggressor = None

    def _advance_street(self) -> None:
        self._collect_bets()

        if self.street is Street.RIVER:
            self._finish(went_to_showdown=True)
            return

        next_street = STREET_ORDER[STREET_ORDER.index(self.street) + 1]
        self.street = next_street

        burned = self.deck.burn() if self.burn_cards else None
        if next_street is Street.FLOP:
            new_cards = self.deck.draw(3)
        else:
            new_cards = self.deck.draw(1)
        self.board.extend(new_cards)

        self._emit(
            "street",
            {
                "street": next_street.value,
                "cards": new_cards,
                "board": list(self.board),
                "pot": self.pot_total,
                "burned": burned,
            },
        )

        # если торговаться уже некому (все олл-ин) — просто открываем следующую улицу
        if not self._can_still_bet():
            self._advance_street()
            return

        first = self._first_to_act_postflop()
        if first is None:
            self._advance_street()
            return
        self.to_act = first
        self._request_action()

    def _can_still_bet(self) -> bool:
        """Есть ли минимум два игрока со стеком, способных торговаться."""
        able = [
            s for s in self.active_seats
            if self.seats[s].state == SeatState.IN_HAND and self.seats[s].stack > 0
        ]
        return len(able) >= 2

    def _first_to_act_postflop(self) -> int | None:
        return self._first_actor_from(self._next_participant(self.button_seat))

    # ------------------------------------------------------------------ finish
    def _finish(self, *, went_to_showdown: bool) -> None:
        if self.finished:
            return
        self.finished = True
        self.to_act = None

        contributions = {s: self.seats[s].committed for s in self.participants}
        active = self.active_seats
        layout = build_pots(contributions, active)

        # неколированный излишек возвращается в стек до вскрытия
        for seat_no, amount in layout.uncalled_returns.items():
            self.seats[seat_no].stack += amount
            self.seats[seat_no].committed -= amount
            if amount:
                self._emit(
                    "uncalled_bet_returned",
                    {"seat_no": seat_no, "amount": amount, "stack": self.seats[seat_no].stack},
                )

        ranks: dict[int, HandRank] = {}
        showdown: dict[int, tuple[list[str], str]] = {}
        if went_to_showdown and len(active) > 1:
            for seat_no in active:
                seat = self.seats[seat_no]
                rank = best_hand(seat.hole_cards, self.board)
                ranks[seat_no] = rank
                showdown[seat_no] = (list(seat.hole_cards), rank.describe())
            self._emit(
                "showdown",
                {
                    "board": list(self.board),
                    "hands": [
                        {
                            "seat_no": s,
                            "player_id": self.seats[s].player_id,
                            "cards": showdown[s][0],
                            "combination": showdown[s][1],
                            "best_five": list(ranks[s].best_five),
                        }
                        for s in active
                    ],
                },
            )
        else:
            # банк забирает единственный оставшийся без вскрытия;
            # его карты всё равно показываем в hand_finished
            for seat_no in active:
                ranks[seat_no] = _WALKOVER

        odd_order = [
            s.seat_no for s in self._seats_from(self.button_seat) if s.seat_no in self.participants
        ]
        awards = distribute(layout, ranks, odd_order)
        for award in awards:
            self.seats[award.seat_no].stack += award.amount

        result = HandResult(
            hand_id=self.hand_id,
            board=list(self.board),
            awards=awards,
            showdown=showdown,
            returns=dict(layout.uncalled_returns),
            total_pot=layout.total,
            went_to_showdown=bool(showdown),
        )
        self.result = result

        self._emit(
            "hand_finished",
            {
                "hand_id": self.hand_id,
                "board": list(self.board),
                "total_pot": layout.total,
                "went_to_showdown": result.went_to_showdown,
                "pots": [
                    {"index": p.index, "amount": p.amount, "eligible": sorted(p.eligible_seats)}
                    for p in layout.pots
                ],
                "winners": [
                    {
                        "seat_no": a.seat_no,
                        "player_id": self.seats[a.seat_no].player_id,
                        "amount": a.amount,
                        "pot_index": a.pot_index,
                        "combination": showdown.get(a.seat_no, (None, None))[1],
                        # карты победителя видят все — и после вскрытия, и когда остальные спасовали
                        "cards": list(self.seats[a.seat_no].hole_cards),
                    }
                    for a in awards
                ],
                "stacks": {s: self.seats[s].stack for s in self.participants},
            },
        )

        for seat_no in self.participants:
            seat = self.seats[seat_no]
            seat.bet = 0
            if seat.state != SeatState.FOLDED:
                seat.state = SeatState.WAITING if seat.stack > 0 else SeatState.SITTING_OUT


class _Walkover:
    """Заглушка силы руки, когда вскрытия не было (все спасовали)."""

    def __gt__(self, other: object) -> bool:  # pragma: no cover
        return False

    def __lt__(self, other: object) -> bool:  # pragma: no cover
        return False

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Walkover)

    def __hash__(self) -> int:
        return hash("walkover")


_WALKOVER = _Walkover()
