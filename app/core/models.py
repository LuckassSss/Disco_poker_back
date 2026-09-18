"""Модели стола и места, общие для игрового ядра и сетевого слоя."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.core.enums import SeatState


@dataclass(slots=True)
class Seat:
    """Место за столом. Живёт всё время жизни стола, игрок в него садится и встаёт."""

    seat_no: int
    player_id: str | None = None
    nickname: str = ""
    stack: int = 0
    state: SeatState = SeatState.EMPTY

    # --- состояние в текущей раздаче ---------------------------------------
    hole_cards: list[str] = field(default_factory=list)
    #: ставка на текущей улице
    bet: int = 0
    #: всего вложено за раздачу (для сайд-потов)
    committed: int = 0
    #: игрок был all-in ещё до текущей улицы
    all_in: bool = False

    # --- сетевое состояние --------------------------------------------------
    connected: bool = False
    disconnected_at: float | None = None
    #: игрок сам вышел «постоять» или его вывели после серии авто-фолдов
    sitting_out: bool = False
    #: сит-аут поставил сервер (кончились фишки / серия авто-фолдов), а не игрок —
    #: такой сит-аут снимается автоматически при докупке
    auto_sat_out: bool = False
    auto_folds: int = 0
    #: попросил встать — освободим место сразу после окончания раздачи
    leave_requested: bool = False
    #: докупка, оформленная во время раздачи: попадёт в стек со следующей
    pending_rebuy: int = 0
    joined_at: float = field(default_factory=time.time)

    @property
    def is_occupied(self) -> bool:
        return self.player_id is not None

    @property
    def is_in_hand(self) -> bool:
        return self.state in (SeatState.IN_HAND, SeatState.ALL_IN)

    @property
    def can_be_dealt(self) -> bool:
        """Готов ли игрок участвовать в следующей раздаче."""
        return (
            self.is_occupied
            and not self.sitting_out
            and not self.leave_requested
            and (self.stack + self.pending_rebuy) > 0
        )

    def reset_for_new_hand(self) -> None:
        self.hole_cards = []
        self.bet = 0
        self.committed = 0
        self.all_in = False
        if self.pending_rebuy:
            self.stack += self.pending_rebuy
            self.pending_rebuy = 0
        if self.is_occupied:
            self.state = SeatState.WAITING if self.can_be_dealt else SeatState.SITTING_OUT
        else:
            self.state = SeatState.EMPTY

    def clear(self) -> None:
        self.player_id = None
        self.nickname = ""
        self.stack = 0
        self.state = SeatState.EMPTY
        self.hole_cards = []
        self.bet = 0
        self.committed = 0
        self.all_in = False
        self.connected = False
        self.disconnected_at = None
        self.sitting_out = False
        self.auto_sat_out = False
        self.auto_folds = 0
        self.leave_requested = False
        self.pending_rebuy = 0

    def public_view(self) -> dict:
        return {
            "seat_no": self.seat_no,
            "player_id": self.player_id,
            "nickname": self.nickname,
            "stack": self.stack,
            "state": self.state.value,
            "bet": self.bet,
            "committed": self.committed,
            "connected": self.connected,
            "sitting_out": self.sitting_out,
            "has_cards": bool(self.hole_cards),
        }
