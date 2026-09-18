"""Доступ к данным. Все методы безопасны при выключенной БД — просто ничего не делают."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from app.config import settings
from app.db.session import session_scope

# Модели и SQLAlchemy импортируются лениво: при отсутствии драйвера или БД
# репозитории просто ничего не делают, а игра продолжается на памяти.

log = logging.getLogger(__name__)


class PlayerRepository:
    async def get_or_create(self, player_id: str, nickname: str, is_guest: bool = True) -> dict:
        async with session_scope() as session:
            if session is None:
                return {
                    "id": player_id,
                    "nickname": nickname,
                    "balance": settings.guest_start_balance,
                    "is_guest": is_guest,
                    "persisted": False,
                }
            from app.db.models import Player  # noqa: PLC0415

            player = await session.get(Player, player_id)
            if player is None:
                player = Player(
                    id=player_id,
                    nickname=nickname,
                    is_guest=is_guest,
                    balance=settings.guest_start_balance,
                )
                session.add(player)
                await session.flush()
            else:
                player.nickname = nickname
                player.last_seen_at = dt.datetime.now(dt.timezone.utc)
            return {
                "id": player.id,
                "nickname": player.nickname,
                "balance": player.balance,
                "is_guest": player.is_guest,
                "persisted": True,
            }

    async def adjust_balance(self, player_id: str, delta: int) -> int | None:
        async with session_scope() as session:
            if session is None:
                return None
            from app.db.models import Player  # noqa: PLC0415

            player = await session.get(Player, player_id)
            if player is None:
                return None
            player.balance = max(0, player.balance + delta)
            return player.balance

    async def get_balance(self, player_id: str) -> int | None:
        async with session_scope() as session:
            if session is None:
                return None
            from sqlalchemy import select  # noqa: PLC0415

            from app.db.models import Player  # noqa: PLC0415

            row = await session.execute(select(Player.balance).where(Player.id == player_id))
            value = row.scalar_one_or_none()
            return int(value) if value is not None else None


class HandRepository:
    async def save(self, payload: dict[str, Any]) -> None:
        """Пишет завершённую раздачу. Ошибки логируются, но игру не ломают."""
        async with session_scope() as session:
            if session is None:
                return
            from app.db.models import Hand, HandSeat  # noqa: PLC0415

            try:
                hand = Hand(
                    id=payload["hand_id"],
                    table_id=payload["table_id"],
                    stake_code=payload["stake_code"],
                    deck_id=payload["deck_id"],
                    deck_sequence=payload["deck_sequence"],
                    button_seat=payload["button_seat"],
                    board=payload["board"],
                    total_pot=payload["total_pot"],
                    actions=payload["actions"],
                    winners=payload["winners"],
                    finished_at=dt.datetime.now(dt.timezone.utc),
                )
                session.add(hand)
                for seat in payload["seats"]:
                    session.add(
                        HandSeat(
                            hand_id=hand.id,
                            seat_no=seat["seat_no"],
                            player_id=seat["player_id"],
                            stack_before=seat["stack_before"],
                            stack_after=seat["stack_after"],
                            committed=seat["committed"],
                            hole_cards=seat["hole_cards"],
                            result=seat["stack_after"] - seat["stack_before"],
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                log.exception("Не удалось сохранить раздачу %s: %s", payload.get("hand_id"), exc)
                raise


player_repository = PlayerRepository()
hand_repository = HandRepository()
