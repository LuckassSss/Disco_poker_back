"""Счета игроков.

Баланс — «кошелёк» вне стола. Бай-ин списывает с баланса в стек, выход из-за
стола возвращает стек обратно. Кэш в памяти, чтобы не ходить в БД на каждое
движение фишек; в Postgres пишем итог операции.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.config import settings
from app.db.repositories import player_repository

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Account:
    player_id: str
    nickname: str
    balance: int


class AccountService:
    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._lock = asyncio.Lock()

    async def ensure(self, player_id: str, nickname: str) -> Account:
        async with self._lock:
            account = self._accounts.get(player_id)
            if account is not None:
                account.nickname = nickname
                return account
        try:
            data = await player_repository.get_or_create(player_id, nickname)
        except Exception as exc:  # noqa: BLE001
            # сбой БД не должен мешать войти в игру: продолжаем на памяти
            log.warning("Не удалось прочитать профиль %s из БД: %s", player_id, exc)
            data = {"balance": settings.guest_start_balance}
        async with self._lock:
            account = self._accounts.setdefault(
                player_id,
                Account(
                    player_id=player_id,
                    nickname=nickname,
                    balance=int(data.get("balance") or settings.guest_start_balance),
                ),
            )
            account.nickname = nickname
            return account

    def get(self, player_id: str) -> Account | None:
        return self._accounts.get(player_id)

    async def withdraw(self, player_id: str, amount: int) -> bool:
        """Списывает с баланса. False, если денег не хватает."""
        if amount <= 0:
            return False
        async with self._lock:
            account = self._accounts.get(player_id)
            if account is None or account.balance < amount:
                return False
            account.balance -= amount
        await self._persist(player_id, -amount)
        return True

    async def deposit(self, player_id: str, amount: int) -> int:
        if amount <= 0:
            account = self._accounts.get(player_id)
            return account.balance if account else 0
        async with self._lock:
            account = self._accounts.get(player_id)
            if account is None:
                account = self._accounts[player_id] = Account(player_id, "", 0)
            account.balance += amount
            balance = account.balance
        await self._persist(player_id, amount)
        return balance

    async def _persist(self, player_id: str, delta: int) -> None:
        try:
            await player_repository.adjust_balance(player_id, delta)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось записать баланс игрока %s: %s", player_id, exc)


accounts = AccountService()
