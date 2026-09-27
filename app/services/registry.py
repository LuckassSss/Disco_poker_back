"""Реестр столов и автоподбор.

Автоподбор (``quick_join``) — единственный способ сесть за стол на этом этапе:
клиент присылает уровень ставок, сервер сам находит стол со свободным местом,
а если такого нет — создаёт новый.

Стратегия подбора: из столов нужного уровня со свободным местом берём тот,
где **больше всего игроков**. Так столы «доукомплектовываются», а не размазывают
игроков по десятку полупустых — за 5-max это принципиально для качества игры.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from app.config import StakeLevel, settings
from app.services.table import Table, TableError
from app.protocol.messages import ErrorCode

log = logging.getLogger(__name__)


class TableRegistry:
    def __init__(self) -> None:
        self._tables: dict[str, Table] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None
        #: игроки, для которых прямо сейчас идёт подбор стола. Без этой брони
        #: два одновременных quick_join одного игрока (вторая вкладка, обрыв на
        #: мобильной сети) сажали его за два разных стола и списывали бай-ин дважды
        self._joining: set[str] = set()

    # ---- жизненный цикл ----------------------------------------------------
    async def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop(), name="table-reaper")

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        async with self._lock:
            tables = list(self._tables.values())
            self._tables.clear()
        await asyncio.gather(*(t.stop() for t in tables), return_exceptions=True)

    # ---- доступ ------------------------------------------------------------
    def get(self, table_id: str) -> Table | None:
        return self._tables.get(table_id)

    def all_tables(self) -> list[Table]:
        return list(self._tables.values())

    def find_table_of(self, player_id: str) -> Table | None:
        return self._find_table_of_locked(player_id)

    def _find_table_of_locked(self, player_id: str) -> Table | None:
        for table in self._tables.values():
            if table.find_seat_of(player_id) is not None:
                return table
        return None

    def lobby(self, stake_code: str | None = None) -> list[dict]:
        return [
            t.lobby_view()
            for t in self._tables.values()
            if stake_code is None or t.stake.code == stake_code
        ]

    # ---- автоподбор --------------------------------------------------------
    async def quick_join(
        self, player_id: str, nickname: str, stake_code: str | None, buy_in: int | None
    ) -> tuple[Table, dict]:
        stake = self._resolve_stake(stake_code)

        # бронь игрока и проверка «уже за столом» — под одним локом, иначе два
        # параллельных запроса разъедутся по разным столам
        async with self._lock:
            existing = self._find_table_of_locked(player_id)
            if existing is not None:
                raise TableError(
                    ErrorCode.ALREADY_AT_TABLE,
                    f"Вы уже за столом {existing.table_id}. Сначала покиньте его.",
                )
            if player_id in self._joining:
                raise TableError(
                    ErrorCode.JOIN_IN_PROGRESS, "Подбор стола уже идёт, дождитесь ответа"
                )
            self._joining.add(player_id)

        try:
            # несколько попыток: место могли занять между выбором стола и посадкой
            for attempt in range(5):
                # pending_joins инкрементируется под тем же локом, что и выбор стола,
                # поэтому уборщик не успеет снести стол между выбором и посадкой
                table = await self._pick_or_create(stake)
                try:
                    result = await table.submit(
                        "sit_down",
                        {"player_id": player_id, "nickname": nickname, "buy_in": buy_in},
                        wait=True,
                    )
                    return table, result
                except TableError as exc:
                    if exc.code in (ErrorCode.INTERNAL, ErrorCode.ALREADY_AT_TABLE) and attempt < 4:
                        log.debug("quick_join: повтор подбора (%s)", exc.message)
                        continue
                    raise
                finally:
                    table.pending_joins = max(0, table.pending_joins - 1)
            raise TableError(ErrorCode.INTERNAL, "Не удалось подобрать стол, попробуйте ещё раз")
        finally:
            self._joining.discard(player_id)

    def _resolve_stake(self, stake_code: str | None) -> StakeLevel:
        if stake_code is None:
            return settings.default_stake
        stake = settings.stake(stake_code)
        if stake is None:
            codes = ", ".join(s.code for s in settings.stakes)
            raise TableError(
                ErrorCode.UNKNOWN_STAKE, f"Неизвестный уровень ставок. Доступны: {codes}"
            )
        return stake

    async def _pick_or_create(self, stake: StakeLevel) -> Table:
        """Возвращает стол с уже увеличенным ``pending_joins`` — снимать обязан вызывающий."""
        async with self._lock:
            candidates = [
                t for t in self._tables.values()
                if t.stake.code == stake.code and t.free_seats - t.pending_joins > 0
            ]
            if candidates:
                # самый заполненный стол из тех, где есть место
                candidates.sort(key=lambda t: (-t.occupied_count, t.created_at))
                table = candidates[0]
            else:
                table = self._create_table_locked(stake)
            table.pending_joins += 1
            return table

    def _create_table_locked(self, stake: StakeLevel) -> Table:
        table_id = f"{stake.code}-{uuid.uuid4().hex[:8]}"
        table = Table(table_id=table_id, stake=stake)
        table.start()
        self._tables[table_id] = table
        log.info("Создан стол %s (%s, %s мест)", table_id, stake.code, table.max_seats)
        return table

    # ---- уборка пустых столов ---------------------------------------------
    async def _reap_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(15)
                await self._reap_once()
        except asyncio.CancelledError:
            pass

    async def _reap_once(self) -> None:
        now = time.monotonic()
        doomed: list[Table] = []
        async with self._lock:
            for table in list(self._tables.values()):
                if not table.is_empty or table.pending_joins > 0:
                    table.empty_since = None
                    continue
                if table.empty_since is None:
                    table.empty_since = now
                    continue
                if now - table.empty_since >= settings.empty_table_ttl_seconds:
                    doomed.append(table)
                    self._tables.pop(table.table_id, None)
        for table in doomed:
            log.info("Стол %s удалён: пустует дольше %.0f с",
                     table.table_id, settings.empty_table_ttl_seconds)
            await table.stop()

    @property
    def stats(self) -> dict:
        tables = self.all_tables()
        return {
            "tables": len(tables),
            "players_seated": sum(t.occupied_count for t in tables),
            "by_stake": {
                stake.code: sum(1 for t in tables if t.stake.code == stake.code)
                for stake in settings.stakes
            },
        }


registry = TableRegistry()
