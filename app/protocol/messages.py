"""Протокол обмена по WebSocket.

Формат конверта одинаков в обе стороны::

    {"t": "<тип>", "d": {...}, "rid": "<необязательный id запроса>"}

``rid`` клиент ставит сам; сервер возвращает его в ответе или в ошибке, чтобы
Unity-клиент мог связать ответ с запросом. Все суммы — целые фишки.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ============================================================ клиент -> сервер
class ClientMessageType(str, Enum):
    AUTH = "auth"
    PING = "ping"
    LIST_STAKES = "list_stakes"
    QUICK_JOIN = "quick_join"
    LEAVE_TABLE = "leave_table"
    SIT_OUT = "sit_out"
    SIT_IN = "sit_in"
    REBUY = "rebuy"
    ACTION = "action"
    REQUEST_SNAPSHOT = "request_snapshot"
    CHAT = "chat"


class Envelope(BaseModel):
    t: ClientMessageType
    d: dict[str, Any] = Field(default_factory=dict)
    rid: str | None = None


class AuthPayload(BaseModel):
    token: str


class QuickJoinPayload(BaseModel):
    """Автоподбор: сервер сам находит стол со свободным местом внутри уровня ставок."""

    stake: str | None = None
    buy_in: int | None = None

    @field_validator("buy_in")
    @classmethod
    def _positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("buy_in должен быть положительным")
        return v


class ActionPayload(BaseModel):
    hand_id: str | None = None
    action: Literal["fold", "check", "call", "bet", "raise", "all_in"]
    #: для bet/raise — суммарная ставка «до» на этой улице, а не приращение
    amount: int = 0


class RebuyPayload(BaseModel):
    amount: int


class ChatPayload(BaseModel):
    text: str = Field(max_length=200)


# ============================================================ сервер -> клиент
class ServerMessageType(str, Enum):
    WELCOME = "welcome"
    PONG = "pong"
    ERROR = "error"
    STAKES = "stakes"

    JOINED_TABLE = "joined_table"
    LEFT_TABLE = "left_table"
    TABLE_SNAPSHOT = "table_snapshot"
    SEAT_UPDATE = "seat_update"
    PLAYER_JOINED = "player_joined"
    PLAYER_LEFT = "player_left"
    TABLE_STATE = "table_state"

    HAND_STARTED = "hand_started"
    HOLE_CARDS = "hole_cards"
    CARDS_DEALT = "cards_dealt"
    BLIND_POSTED = "blind_posted"
    ACTION_REQUIRED = "action_required"
    PLAYER_ACTION = "player_action"
    ACTION_TIMEOUT = "action_timeout"
    STREET = "street"
    SHOWDOWN = "showdown"
    UNCALLED_BET_RETURNED = "uncalled_bet_returned"
    HAND_FINISHED = "hand_finished"
    CHAT = "chat"


class ErrorCode(str, Enum):
    UNAUTHORIZED = "unauthorized"
    BAD_MESSAGE = "bad_message"
    NOT_AT_TABLE = "not_at_table"
    ALREADY_AT_TABLE = "already_at_table"
    #: предыдущий quick_join этого же игрока ещё выполняется
    JOIN_IN_PROGRESS = "join_in_progress"
    UNKNOWN_STAKE = "unknown_stake"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    INVALID_BUY_IN = "invalid_buy_in"
    INVALID_ACTION = "invalid_action"
    NOT_YOUR_TURN = "not_your_turn"
    RATE_LIMITED = "rate_limited"
    INTERNAL = "internal"


def out(type_: ServerMessageType | str, data: dict | None = None, rid: str | None = None) -> dict:
    """Собирает конверт ответа."""
    payload: dict[str, Any] = {
        "t": type_.value if isinstance(type_, ServerMessageType) else str(type_),
        "d": data or {},
    }
    if rid:
        payload["rid"] = rid
    return payload


def error(code: ErrorCode, message: str, rid: str | None = None) -> dict:
    return out(ServerMessageType.ERROR, {"code": code.value, "message": message}, rid)
