"""Схема БД. На этом этапе минимум: игроки, история раздач, лог выдачи колод."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Player(Base):
    __tablename__ = "players"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(32), nullable=False)
    is_guest: Mapped[bool] = mapped_column(default=True, nullable=False)
    #: у гостей пусто; у зарегистрированных — уникальный логин и scrypt-хэш пароля
    login: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: jti текущего токена; всё, что выдано раньше, отозвано
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Hand(Base):
    """Одна сыгранная раздача. Пишется одним апдейтом по завершении."""

    __tablename__ = "hands"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    table_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stake_code: Mapped[str] = mapped_column(String(16), nullable=False)
    #: индекс колоды в data/decks.json и сквозной номер выдачи — для аудита честности
    deck_id: Mapped[int] = mapped_column(Integer, nullable=False)
    deck_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    button_seat: Mapped[int] = mapped_column(Integer, nullable=False)
    board: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    total_pot: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: полный лог действий раздачи
    actions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    winners: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    seats: Mapped[list["HandSeat"]] = relationship(back_populates="hand", lazy="selectin")


class HandSeat(Base):
    __tablename__ = "hand_seats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hand_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("hands.id", ondelete="CASCADE"), nullable=False
    )
    seat_no: Mapped[int] = mapped_column(Integer, nullable=False)
    player_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stack_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stack_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    committed: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    hole_cards: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    result: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    hand: Mapped[Hand] = relationship(back_populates="seats")


Index("ix_hand_seats_hand_seat", HandSeat.hand_id, HandSeat.seat_no, unique=True)
