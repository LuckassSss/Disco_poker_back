"""Стол как актор.

Каждый стол — отдельная asyncio-задача со своей очередью команд. Всё состояние
стола меняется только внутри этой задачи, поэтому блокировок нет вообще: гонок
между «игрок сходил», «сработал таймер» и «игрок отключился» физически не может
возникнуть, они выстраиваются в очередь.

Схема::

    WebSocket-хендлер --submit(cmd)--> [очередь] --> Table.run() --> события --> hub --> клиенты
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import StakeLevel, settings
from app.core.deck_provider import deck_provider
from app.core.enums import ActionType, SeatState, TableState
from app.core.game import Event, HandEngine, InvalidAction
from app.core.models import Seat
from app.protocol.messages import ErrorCode, ServerMessageType, out
from app.services.accounts import accounts
from app.services.hub import hub

log = logging.getLogger(__name__)

HOUSEKEEPING_INTERVAL = 5.0


@dataclass(slots=True)
class Command:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    future: asyncio.Future | None = None


class TableError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Table:
    def __init__(self, table_id: str, stake: StakeLevel, max_seats: int | None = None) -> None:
        self.table_id = table_id
        self.stake = stake
        self.max_seats = max_seats or settings.max_seats
        self.seats: list[Seat] = [Seat(seat_no=i) for i in range(self.max_seats)]
        self.button_seat: int = 0
        self.state: TableState = TableState.WAITING_FOR_PLAYERS
        self.hand_no: int = 0

        self.engine: HandEngine | None = None
        self._inbox: asyncio.Queue[Command] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._closing = False

        self._action_deadline: float | None = None
        self._acting_seat: int | None = None
        self._next_hand_at: float | None = None
        self._housekeeping_at: float = time.monotonic() + HOUSEKEEPING_INTERVAL
        self._hand_log: list[dict] = []
        self._stacks_before: dict[int, int] = {}
        #: держим ссылки на фоновые задачи, иначе их может собрать сборщик мусора
        self._background: set[asyncio.Task] = set()
        self.created_at = time.time()
        self.empty_since: float | None = time.monotonic()
        #: сколько игроков прямо сейчас в процессе посадки — уборщик пустых
        #: столов обязан их дождаться, иначе стол снесут вместе со списанным бай-ином
        self.pending_joins: int = 0

    # ------------------------------------------------------------------ жизненный цикл
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name=f"table-{self.table_id}")

    async def stop(self) -> None:
        self._closing = True
        await self._inbox.put(Command(kind="__stop__"))
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        self._drain_pending("Стол остановлен")

    def _drain_pending(self, reason: str) -> None:
        """Разрешает зависшие future, чтобы клиенты не ждали ответа вечно.

        Без этого остановка стола (в том числе уборка пустых столов) оставляла бы
        обработчик сокета висеть на ``await`` навсегда.
        """
        while True:
            try:
                command = self._inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if command.future is not None and not command.future.done():
                command.future.set_exception(TableError(ErrorCode.INTERNAL, reason))

    #: страховочный таймаут ожидания ответа стола — клиент не должен висеть вечно,
    #: даже если задача стола умерла в неожиданном месте
    COMMAND_TIMEOUT = 15.0

    @property
    def is_alive(self) -> bool:
        return not self._closing and not (self._task is None or self._task.done())

    async def submit(self, kind: str, payload: dict | None = None, wait: bool = False) -> Any:
        if not self.is_alive:
            raise TableError(ErrorCode.INTERNAL, "Стол недоступен, переподключитесь")
        command = Command(kind=kind, payload=payload or {})
        if wait:
            command.future = asyncio.get_running_loop().create_future()
        await self._inbox.put(command)
        if command.future is not None:
            try:
                return await asyncio.wait_for(command.future, timeout=self.COMMAND_TIMEOUT)
            except asyncio.TimeoutError as exc:
                log.error("Стол %s: команда %s не обработана вовремя", self.table_id, kind)
                raise TableError(ErrorCode.INTERNAL, "Стол не ответил вовремя") from exc
        return None

    async def run(self) -> None:
        log.info("Стол %s запущен (%s)", self.table_id, self.stake.code)
        try:
            while not self._closing:
                timeout = self._time_to_next_deadline()
                try:
                    command = await asyncio.wait_for(self._inbox.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    await self._safe_deadline()
                    continue
                if command.kind == "__stop__":
                    break
                try:
                    result = await self._handle(command)
                    if command.future is not None and not command.future.done():
                        command.future.set_result(result)
                except TableError as exc:
                    if command.future is not None and not command.future.done():
                        command.future.set_exception(exc)
                    else:
                        await self._send_error(command, exc)
                except Exception:  # noqa: BLE001
                    log.exception("Стол %s: ошибка обработки %s", self.table_id, command.kind)
                    if command.future is not None and not command.future.done():
                        command.future.set_exception(
                            TableError(ErrorCode.INTERNAL, "Внутренняя ошибка стола")
                        )
                # Дедлайны проверяем и здесь: иначе поток команд от клиентов
                # (снапшоты, чат) не давал бы сработать таймеру хода и запуску
                # следующей раздачи — очередь никогда не пустела бы.
                await self._safe_deadline()
        except asyncio.CancelledError:
            raise
        finally:
            self._drain_pending("Стол остановлен")
            log.info("Стол %s остановлен", self.table_id)

    async def _safe_deadline(self) -> None:
        """Обработка таймеров не должна ронять задачу стола."""
        try:
            await self._on_deadline()
        except Exception:  # noqa: BLE001
            log.exception("Стол %s: ошибка в обработке таймера", self.table_id)
            # раздачу не запускаем сразу заново, иначе рискуем зациклиться на ошибке
            self._action_deadline = None
            self._next_hand_at = time.monotonic() + max(2.0, settings.inter_hand_delay_seconds)

    def _time_to_next_deadline(self) -> float:
        now = time.monotonic()
        candidates = [self._housekeeping_at]
        if self._action_deadline is not None:
            candidates.append(self._action_deadline)
        if self._next_hand_at is not None:
            candidates.append(self._next_hand_at)
        return max(0.05, min(candidates) - now)

    async def _on_deadline(self) -> None:
        now = time.monotonic()
        if self._action_deadline is not None and now >= self._action_deadline:
            await self._force_action()
        if self._next_hand_at is not None and now >= self._next_hand_at:
            self._next_hand_at = None
            await self._try_start_hand()
        if now >= self._housekeeping_at:
            self._housekeeping_at = now + HOUSEKEEPING_INTERVAL
            await self._housekeeping()

    # ------------------------------------------------------------------ команды
    async def _handle(self, command: Command) -> Any:
        handler = getattr(self, f"_cmd_{command.kind}", None)
        if handler is None:
            raise TableError(ErrorCode.BAD_MESSAGE, f"Неизвестная команда {command.kind}")
        return await handler(command.payload)

    async def _send_error(self, command: Command, exc: TableError) -> None:
        player_id = command.payload.get("player_id")
        if player_id:
            await hub.send(
                player_id,
                out(
                    ServerMessageType.ERROR,
                    {"code": exc.code.value, "message": exc.message},
                    command.payload.get("rid"),
                ),
            )

    # ---- посадка -----------------------------------------------------------
    async def _cmd_sit_down(self, payload: dict) -> dict:
        player_id: str = payload["player_id"]
        nickname: str = payload["nickname"]
        buy_in: int | None = payload.get("buy_in")

        if self.find_seat_of(player_id) is not None:
            raise TableError(ErrorCode.ALREADY_AT_TABLE, "Вы уже за этим столом")

        seat = next((s for s in self.seats if not s.is_occupied), None)
        if seat is None:
            raise TableError(ErrorCode.INTERNAL, "Свободных мест нет")

        amount = buy_in or self.stake.max_buy_in
        if not (self.stake.min_buy_in <= amount <= self.stake.max_buy_in):
            raise TableError(
                ErrorCode.INVALID_BUY_IN,
                f"Бай-ин должен быть от {self.stake.min_buy_in} до {self.stake.max_buy_in}",
            )
        account = await accounts.ensure(player_id, nickname)
        if account.balance < amount:
            # не хватает на полный бай-ин — садим на всё, что есть, если это не меньше минимума
            if account.balance < self.stake.min_buy_in:
                raise TableError(
                    ErrorCode.INSUFFICIENT_FUNDS,
                    f"На балансе {account.balance}, нужен минимум {self.stake.min_buy_in}",
                )
            amount = account.balance
        if not await accounts.withdraw(player_id, amount):
            raise TableError(ErrorCode.INSUFFICIENT_FUNDS, "Недостаточно средств на балансе")

        seat.player_id = player_id
        seat.nickname = nickname
        seat.stack = amount
        seat.connected = True
        seat.sitting_out = False
        seat.leave_requested = False
        seat.auto_folds = 0
        seat.joined_at = time.time()
        seat.state = SeatState.WAITING if self.engine is None else SeatState.SITTING_OUT
        self.empty_since = None

        log.info(
            "Стол %s: игрок %s (%s) сел на место %s с %s фишек",
            self.table_id, nickname, player_id, seat.seat_no, amount,
        )
        await self._broadcast(
            out(ServerMessageType.PLAYER_JOINED, {"seat": seat.public_view()}),
            exclude=player_id,
        )
        await hub.send(player_id, out(ServerMessageType.JOINED_TABLE, self.snapshot(player_id)))
        await self._maybe_schedule_hand()
        return {"seat_no": seat.seat_no, "buy_in": amount}

    async def _cmd_leave(self, payload: dict) -> dict:
        player_id = payload["player_id"]
        seat = self.find_seat_of(player_id)
        if seat is None:
            raise TableError(ErrorCode.NOT_AT_TABLE, "Вы не за этим столом")
        if seat.is_in_hand and self.engine is not None and not self.engine.finished:
            # уходим красиво: досрочный фолд, место освободим после раздачи
            seat.leave_requested = True
            if self.engine.to_act == seat.seat_no:
                await self._apply_action(seat.seat_no, ActionType.FOLD, 0, auto=True)
            else:
                seat.state = SeatState.FOLDED
                await self._flush()
                await self._check_hand_end_after_external_fold()
            return {"deferred": True}
        await self._release_seat(seat, reason="left")
        return {"deferred": False}

    async def _cmd_sit_out(self, payload: dict) -> dict:
        seat = self._require_seat(payload["player_id"])
        seat.sitting_out = True
        seat.auto_sat_out = False  # решение игрока, а не сервера
        if not seat.is_in_hand:
            seat.state = SeatState.SITTING_OUT
        await self._broadcast(out(ServerMessageType.SEAT_UPDATE, {"seat": seat.public_view()}))
        return {"sitting_out": True}

    async def _cmd_sit_in(self, payload: dict) -> dict:
        seat = self._require_seat(payload["player_id"])
        seat.sitting_out = False
        seat.auto_sat_out = False
        seat.auto_folds = 0
        if not seat.is_in_hand:
            seat.state = SeatState.WAITING if seat.stack > 0 else SeatState.SITTING_OUT
        await self._broadcast(out(ServerMessageType.SEAT_UPDATE, {"seat": seat.public_view()}))
        await self._maybe_schedule_hand()
        return {"sitting_out": False}

    async def _cmd_rebuy(self, payload: dict) -> dict:
        seat = self._require_seat(payload["player_id"])
        amount = int(payload.get("amount") or 0)
        if amount <= 0:
            raise TableError(ErrorCode.INVALID_BUY_IN, "Сумма докупки должна быть больше нуля")
        if seat.stack + seat.pending_rebuy + amount > self.stake.max_buy_in:
            raise TableError(
                ErrorCode.INVALID_BUY_IN,
                f"Стек не может превышать {self.stake.max_buy_in}",
            )
        if not await accounts.withdraw(seat.player_id, amount):
            raise TableError(ErrorCode.INSUFFICIENT_FUNDS, "Недостаточно средств на балансе")

        # сит-аут, который поставил сервер из-за нулевого стека или серии
        # авто-фолдов, докупка снимает сама: иначе игрок остался бы вне игры,
        # а хедз-ап стол после вылета соперника не раздавал бы вообще
        if seat.auto_sat_out:
            seat.sitting_out = False
            seat.auto_sat_out = False
            seat.auto_folds = 0

        if seat.is_in_hand:
            # менять стек посреди раздачи нельзя — докупка встанет в силу со следующей
            seat.pending_rebuy += amount
            pending = True
        else:
            seat.stack += amount
            pending = False
            if seat.stack > 0 and not seat.sitting_out and seat.state is SeatState.SITTING_OUT:
                seat.state = SeatState.WAITING

        await self._broadcast(
            out(ServerMessageType.SEAT_UPDATE, {"seat": seat.public_view(), "pending_rebuy": seat.pending_rebuy})
        )
        await self._maybe_schedule_hand()
        return {"stack": seat.stack, "pending": pending, "pending_rebuy": seat.pending_rebuy}

    # ---- ход ---------------------------------------------------------------
    async def _cmd_action(self, payload: dict) -> dict:
        player_id = payload["player_id"]
        seat = self._require_seat(player_id)
        if self.engine is None or self.engine.finished:
            raise TableError(ErrorCode.INVALID_ACTION, "Сейчас нет активной раздачи")
        if self.engine.to_act != seat.seat_no:
            raise TableError(ErrorCode.NOT_YOUR_TURN, "Сейчас не ваш ход")
        hand_id = payload.get("hand_id")
        if hand_id and hand_id != self.engine.hand_id:
            raise TableError(ErrorCode.INVALID_ACTION, "Ход относится к другой раздаче")
        try:
            action = ActionType(payload["action"])
        except ValueError as exc:
            raise TableError(ErrorCode.INVALID_ACTION, "Неизвестное действие") from exc
        await self._apply_action(seat.seat_no, action, int(payload.get("amount") or 0))
        return {"ok": True}

    async def _apply_action(
        self, seat_no: int, action: ActionType, amount: int, auto: bool = False
    ) -> None:
        assert self.engine is not None
        seat = self.seats[seat_no]
        try:
            self.engine.act(seat_no, action, amount)
        except InvalidAction as exc:
            raise TableError(ErrorCode.INVALID_ACTION, str(exc)) from exc
        self._action_deadline = None
        self._acting_seat = None
        if auto:
            seat.auto_folds += 1
            if seat.auto_folds >= settings.auto_fold_limit:
                seat.sitting_out = True
                seat.auto_sat_out = True
                await self._broadcast(
                    out(
                        ServerMessageType.SEAT_UPDATE,
                        {"seat": seat.public_view(), "reason": "auto_fold_limit"},
                    )
                )
        else:
            seat.auto_folds = 0
        await self._flush()

    async def _force_action(self) -> None:
        """Таймер хода истёк — ходим за игрока: чек, если можно, иначе фолд."""
        self._action_deadline = None
        if self.engine is None or self.engine.finished or self.engine.to_act is None:
            return
        seat_no = self.engine.to_act
        seat = self.seats[seat_no]
        options = {a.action for a in self.engine.legal_actions(seat_no)}
        action = ActionType.CHECK if ActionType.CHECK in options else ActionType.FOLD
        log.info(
            "Стол %s: таймаут хода у места %s (%s) → %s",
            self.table_id, seat_no, seat.nickname, action.value,
        )
        await self._broadcast(
            out(
                ServerMessageType.ACTION_TIMEOUT,
                {"seat_no": seat_no, "player_id": seat.player_id, "action": action.value},
            )
        )
        await self._apply_action(seat_no, action, 0, auto=True)

    async def _check_hand_end_after_external_fold(self) -> None:
        """Игрок сбросил карты не своим ходом (вышел из-за стола) — двигаем раздачу."""
        engine = self.engine
        if engine is None or engine.finished:
            return
        if len(engine.active_seats) <= 1:
            engine._finish(went_to_showdown=False)  # noqa: SLF001
            await self._flush()

    # ---- соединение --------------------------------------------------------
    async def _cmd_connected(self, payload: dict) -> dict:
        seat = self.find_seat_of(payload["player_id"])
        if seat is None:
            raise TableError(ErrorCode.NOT_AT_TABLE, "Вы не за этим столом")
        seat.connected = True
        seat.disconnected_at = None
        await hub.send(
            seat.player_id, out(ServerMessageType.TABLE_SNAPSHOT, self.snapshot(seat.player_id))
        )
        await self._broadcast(
            out(ServerMessageType.SEAT_UPDATE, {"seat": seat.public_view()}),
            exclude=seat.player_id,
        )
        log.info("Стол %s: игрок %s вернулся на место %s", self.table_id, seat.nickname, seat.seat_no)
        return {"seat_no": seat.seat_no}

    async def _cmd_disconnected(self, payload: dict) -> dict:
        seat = self.find_seat_of(payload["player_id"])
        if seat is None:
            return {}
        seat.connected = False
        seat.disconnected_at = time.monotonic()
        await self._broadcast(
            out(ServerMessageType.SEAT_UPDATE, {"seat": seat.public_view()}),
            exclude=seat.player_id,
        )
        log.info("Стол %s: игрок %s отключился (место %s)", self.table_id, seat.nickname, seat.seat_no)
        return {}

    async def _cmd_snapshot(self, payload: dict) -> dict:
        player_id = payload["player_id"]
        snapshot = self.snapshot(player_id)
        await hub.send(player_id, out(ServerMessageType.TABLE_SNAPSHOT, snapshot))
        return snapshot

    async def _cmd_chat(self, payload: dict) -> dict:
        seat = self._require_seat(payload["player_id"])
        await self._broadcast(
            out(
                ServerMessageType.CHAT,
                {
                    "seat_no": seat.seat_no,
                    "nickname": seat.nickname,
                    "text": str(payload.get("text", ""))[:200],
                    "ts": time.time(),
                },
            )
        )
        return {}

    # ------------------------------------------------------------------ раздачи
    def _ready_seats(self) -> list[Seat]:
        return [s for s in self.seats if s.can_be_dealt]

    async def _maybe_schedule_hand(self) -> None:
        if self.engine is not None and not self.engine.finished:
            return
        if len(self._ready_seats()) < settings.min_players_to_start:
            self.state = TableState.WAITING_FOR_PLAYERS
            self._next_hand_at = None
            return
        if self._next_hand_at is None:
            self._next_hand_at = time.monotonic() + settings.inter_hand_delay_seconds

    async def _try_start_hand(self) -> None:
        if self.engine is not None and not self.engine.finished:
            return

        # сброс делаем до подсчёта готовых: здесь же в стек попадают докупки,
        # оформленные во время предыдущей раздачи
        for seat in self.seats:
            seat.reset_for_new_hand()

        ready = self._ready_seats()
        if len(ready) < settings.min_players_to_start:
            self.state = TableState.WAITING_FOR_PLAYERS
            await self._broadcast(
                out(
                    ServerMessageType.TABLE_STATE,
                    {"state": self.state.value, "reason": "waiting_for_players"},
                )
            )
            return

        self.button_seat = self._advance_button([s.seat_no for s in ready])
        self.hand_no += 1
        hand_id = f"{self.table_id}-{self.hand_no}-{uuid.uuid4().hex[:6]}"
        cursor = await deck_provider.next_cursor()

        self._stacks_before = {s.seat_no: s.stack for s in ready}
        self._hand_log = []
        self.engine = HandEngine(
            hand_id=hand_id,
            table_id=self.table_id,
            seats=[s for s in self.seats if s.is_occupied],
            button_seat=self.button_seat,
            small_blind=self.stake.small_blind,
            big_blind=self.stake.big_blind,
            deck=cursor,
            burn_cards=settings.burn_cards,
        )
        self.state = TableState.HAND_IN_PROGRESS
        log.info(
            "Стол %s: старт раздачи %s, колода #%s (выдача %s), баттон место %s, игроков %s",
            self.table_id, hand_id, cursor.deck_id, cursor.sequence, self.button_seat, len(ready),
        )
        try:
            self.engine.start()
            await self._flush()
        except Exception:
            # Раздача не поднялась. Обязательно сбрасываем движок: иначе стол
            # навсегда останется в состоянии «идёт раздача» и больше не раздаст
            # ни одной руки — тихий ступор хуже громкой ошибки.
            log.exception("Стол %s: раздача %s не стартовала, откатываю", self.table_id, hand_id)
            self.engine = None
            self.state = TableState.WAITING_FOR_PLAYERS
            self._action_deadline = None
            self._acting_seat = None
            # возвращаем стеки к состоянию до блайндов, чтобы фишки не потерялись
            for seat_no, stack in self._stacks_before.items():
                self.seats[seat_no].stack = stack
            for seat in self.seats:
                seat.reset_for_new_hand()
            # клиенты могли увидеть hand_started/hole_cards раздачи, которой не будет —
            # шлём снимок, чтобы картинка за столом не осталась фантомной
            for seat in self.seats:
                if seat.player_id:
                    await hub.send(
                        seat.player_id,
                        out(ServerMessageType.TABLE_SNAPSHOT, self.snapshot(seat.player_id)),
                    )
            raise

    def _advance_button(self, ready_seats: list[int]) -> int:
        """Двигает баттон на следующее занятое место по часовой стрелке."""
        if not ready_seats:
            return self.button_seat
        order = sorted(ready_seats)
        for offset in range(1, self.max_seats + 1):
            candidate = (self.button_seat + offset) % self.max_seats
            if candidate in order:
                return candidate
        return order[0]

    # ------------------------------------------------------------------ события
    async def _flush(self) -> None:
        if self.engine is None:
            return
        for event in self.engine.drain_events():
            await self._dispatch(event)
        if self.engine.finished and self.state is TableState.HAND_IN_PROGRESS:
            await self._on_hand_finished()

    async def _dispatch(self, event: Event) -> None:
        self._log_event(event)

        if event.private_to:
            await hub.send(event.private_to, out(event.type, event.data))
            return

        if event.type == "action_required":
            await self._dispatch_action_required(event)
            return

        await self._broadcast(out(event.type, event.data))

    async def _dispatch_action_required(self, event: Event) -> None:
        seat_no = int(event.data["seat_no"])
        seat = self.seats[seat_no]
        timeout = settings.action_timeout_seconds
        if not seat.connected:
            timeout += settings.disconnect_grace_seconds
        self._action_deadline = time.monotonic() + timeout
        self._acting_seat = seat_no

        payload = dict(event.data)
        payload["time_limit"] = timeout
        payload["deadline_ts"] = time.time() + timeout
        await hub.send(seat.player_id, out(ServerMessageType.ACTION_REQUIRED, payload))

        public = {
            k: payload[k]
            for k in ("seat_no", "player_id", "hand_id", "street", "to_call", "pot",
                      "time_limit", "deadline_ts")
            if k in payload
        }
        await self._broadcast(
            out(ServerMessageType.ACTION_REQUIRED, public), exclude=seat.player_id
        )

    def _log_event(self, event: Event) -> None:
        if event.type in ("player_action", "blind_posted", "street", "hand_finished"):
            self._hand_log.append({"type": event.type, **event.data})

    async def _on_hand_finished(self) -> None:
        engine = self.engine
        assert engine is not None
        self.state = TableState.WAITING_FOR_PLAYERS
        self._action_deadline = None
        self._acting_seat = None

        if settings.persist_hand_history:
            # payload собираем ПРЯМО СЕЙЧАС: ниже места освобождаются и очищаются,
            # а фоновая задача успела бы прочитать уже пустые места
            payload = self._build_hand_record(engine)
            if payload is not None:
                task = asyncio.create_task(self._persist_hand(payload))
                self._background.add(task)
                task.add_done_callback(self._background.discard)

        # освобождаем места тех, кто просил выйти, и тех, у кого кончились фишки
        for seat in list(self.seats):
            if not seat.is_occupied:
                continue
            if seat.leave_requested:
                await self._release_seat(seat, reason="left")
            elif seat.stack + seat.pending_rebuy <= 0:
                seat.sitting_out = True
                seat.auto_sat_out = True
                seat.state = SeatState.SITTING_OUT
                await self._broadcast(
                    out(
                        ServerMessageType.SEAT_UPDATE,
                        {"seat": seat.public_view(), "reason": "out_of_chips"},
                    )
                )

        self.engine = None
        await self._maybe_schedule_hand()

    def _build_hand_record(self, engine: HandEngine) -> dict | None:
        result = engine.result
        if result is None:
            return None
        return {
            "hand_id": engine.hand_id,
            "table_id": self.table_id,
            "stake_code": self.stake.code,
            "deck_id": engine.deck.deck_id,
            "deck_sequence": engine.deck.sequence,
            "button_seat": engine.button_seat,
            "board": list(result.board),
            "total_pot": result.total_pot,
            "actions": list(self._hand_log),
            "winners": [
                {"seat_no": a.seat_no, "amount": a.amount, "pot_index": a.pot_index}
                for a in result.awards
            ],
            "seats": [
                {
                    "seat_no": s,
                    "player_id": engine.seats[s].player_id or "",
                    "stack_before": self._stacks_before.get(s, 0),
                    "stack_after": engine.seats[s].stack,
                    "committed": engine.seats[s].committed,
                    "hole_cards": list(engine.seats[s].hole_cards),
                }
                for s in engine.participants
            ],
        }

    async def _persist_hand(self, payload: dict) -> None:
        from app.db.repositories import hand_repository

        try:
            await hand_repository.save(payload)
        except Exception:  # noqa: BLE001
            log.exception("Стол %s: не сохранилась раздача %s", self.table_id, payload["hand_id"])

    # ------------------------------------------------------------------ сервис
    async def _housekeeping(self) -> None:
        now = time.monotonic()
        for seat in self.seats:
            if not seat.is_occupied or seat.connected or seat.disconnected_at is None:
                continue
            offline_for = now - seat.disconnected_at
            if offline_for < settings.reconnect_window_seconds:
                continue
            if seat.is_in_hand and self.engine is not None and not self.engine.finished:
                seat.leave_requested = True  # выйдет сразу после раздачи
                continue
            log.info(
                "Стол %s: место %s освобождено — игрок %s не вернулся за %.0f с",
                self.table_id, seat.seat_no, seat.nickname, offline_for,
            )
            await self._release_seat(seat, reason="reconnect_timeout")
        if not any(s.is_occupied for s in self.seats):
            if self.empty_since is None:
                self.empty_since = now
        else:
            self.empty_since = None

    async def _release_seat(self, seat: Seat, reason: str) -> None:
        player_id = seat.player_id
        # возвращаем на баланс и стек, и неиспользованную докупку
        stack = seat.stack + seat.pending_rebuy
        seat_no = seat.seat_no
        if player_id and stack > 0:
            await accounts.deposit(player_id, stack)
        seat.clear()
        if player_id:
            await hub.send(
                player_id,
                out(
                    ServerMessageType.LEFT_TABLE,
                    {"table_id": self.table_id, "seat_no": seat_no,
                     "returned": stack, "reason": reason},
                ),
            )
            connection = hub.get(player_id)
            if connection is not None and connection.table_id == self.table_id:
                connection.table_id = None
        await self._broadcast(
            out(
                ServerMessageType.PLAYER_LEFT,
                {"seat_no": seat_no, "player_id": player_id, "reason": reason},
            )
        )
        await self._maybe_schedule_hand()

    async def _broadcast(self, message: dict, exclude: str | None = None) -> None:
        targets = [
            s.player_id
            for s in self.seats
            if s.player_id and s.player_id != exclude
        ]
        if targets:
            await hub.send_many(targets, message)

    # ------------------------------------------------------------------ доступ
    def find_seat_of(self, player_id: str) -> Seat | None:
        return next((s for s in self.seats if s.player_id == player_id), None)

    def _require_seat(self, player_id: str) -> Seat:
        seat = self.find_seat_of(player_id)
        if seat is None:
            raise TableError(ErrorCode.NOT_AT_TABLE, "Вы не за этим столом")
        return seat

    @property
    def occupied_count(self) -> int:
        return sum(1 for s in self.seats if s.is_occupied)

    @property
    def free_seats(self) -> int:
        return self.max_seats - self.occupied_count

    @property
    def is_empty(self) -> bool:
        return self.occupied_count == 0

    def snapshot(self, for_player_id: str | None = None) -> dict:
        engine = self.engine
        hand: dict | None = None
        if engine is not None and not engine.finished:
            hand = {
                "hand_id": engine.hand_id,
                "street": engine.street.value,
                "board": list(engine.board),
                "pot": engine.pot_total,
                "current_bet": engine.current_bet,
                "min_raise": engine.min_raise,
                "to_act": engine.to_act,
                "button_seat": engine.button_seat,
                "deck_sequence": engine.deck.sequence,
                "deck_id": engine.deck.deck_id,
            }
            if engine.to_act is not None:
                actor = self.seats[engine.to_act]
                hand["to_act_player_id"] = actor.player_id
                if self._action_deadline is not None:
                    hand["deadline_in"] = max(0.0, self._action_deadline - time.monotonic())

        you: dict | None = None
        if for_player_id:
            seat = self.find_seat_of(for_player_id)
            if seat is not None:
                account = accounts.get(for_player_id)
                you = {
                    "seat_no": seat.seat_no,
                    "stack": seat.stack,
                    "hole_cards": list(seat.hole_cards),
                    "state": seat.state.value,
                    "sitting_out": seat.sitting_out,
                    "balance": account.balance if account else None,
                    "options": (
                        [a.as_dict() for a in engine.legal_actions(seat.seat_no)]
                        if engine is not None and engine.to_act == seat.seat_no
                        else []
                    ),
                }

        return {
            "table": {
                "table_id": self.table_id,
                "stake": self.stake.as_dict,
                "max_seats": self.max_seats,
                "state": self.state.value,
                "button_seat": self.button_seat,
                "hand_no": self.hand_no,
            },
            "seats": [s.public_view() for s in self.seats],
            "hand": hand,
            "you": you,
            "server_time": time.time(),
        }

    def lobby_view(self) -> dict:
        return {
            "table_id": self.table_id,
            "stake": self.stake.code,
            "players": self.occupied_count,
            "max_seats": self.max_seats,
            "state": self.state.value,
        }
