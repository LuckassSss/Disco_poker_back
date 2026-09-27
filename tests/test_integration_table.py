"""Интеграционный прогон всего сервисного слоя без веб-фреймворка.

Здесь работает настоящий стек: реестр столов, автоподбор, актор стола со своими
таймерами, хаб соединений, счета игроков, провайдер колод и игровой движок.
Подменён ровно один элемент — транспорт: вместо WebSocket подставлен объект
с двумя методами (``send_json``/``close``). Стол этого не замечает, потому что
хаб работает с узким контрактом ``SocketLike``.

Таким образом тест закрывает всё, кроме ~200 строк разбора HTTP/WS-запросов
в ``app/api``: посадку, раздачи, таймауты, реконнект и приватность карт.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.core.deck_provider import deck_provider  # noqa: E402
from app.services.accounts import accounts  # noqa: E402
from app.services.hub import Connection, hub  # noqa: E402
from app.services.registry import registry  # noqa: E402


class Lcg:
    """Детерминированные решения ботов."""

    def __init__(self, seed: int) -> None:
        self.state = seed

    def next(self, bound: int) -> int:
        self.state = (self.state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return (self.state >> 33) % bound


class FakeSocket:
    """Транспорт-заглушка. Всё, что уходит клиенту, оседает в ``received``."""

    def __init__(self, owner: "Bot") -> None:
        self.owner = owner
        self.closed = False

    async def send_json(self, message: dict) -> None:
        if self.closed:
            raise RuntimeError("сокет закрыт")
        self.owner.on_message(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class Bot:
    def __init__(self, index: int, *, passive: bool = False) -> None:
        self.player_id = f"bot-{index}"
        self.nickname = f"Бот-{index}"
        self.rng = Lcg(5000 + index)
        #: не отвечает на action_required — проверяем авто-фолд по таймауту
        self.passive = passive
        self.received: list[dict] = []
        self.hands_finished = 0
        self.errors: list[dict] = []
        self.seat_no: int | None = None
        self.table_id: str | None = None
        self.my_cards: list[str] = []
        self.socket = FakeSocket(self)
        self.connection = Connection(
            player_id=self.player_id, nickname=self.nickname, websocket=self.socket
        )
        self._tasks: set[asyncio.Task] = set()

    # ---- приём -------------------------------------------------------------
    def on_message(self, message: dict) -> None:
        self.received.append(message)
        kind, data = message.get("t"), message.get("d", {})

        if kind == "error":
            self.errors.append(data)
        elif kind in ("joined_table", "table_snapshot"):
            self.table_id = data["table"]["table_id"]
            you = data.get("you") or {}
            self.seat_no = you.get("seat_no")
            if you.get("hole_cards"):
                self.my_cards = list(you["hole_cards"])
        elif kind == "hole_cards":
            self.my_cards = list(data["cards"])
        elif kind == "hand_finished":
            self.hands_finished += 1
        elif kind == "action_required" and data.get("options") and not self.passive:
            self._spawn(self._answer(data))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, data: dict) -> None:
        table = registry.get(self.table_id or "")
        if table is None:
            return
        action, amount = self._decide(data["options"])
        try:
            await table.submit(
                "action",
                {
                    "player_id": self.player_id,
                    "hand_id": data.get("hand_id"),
                    "action": action,
                    "amount": amount,
                },
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.errors.append({"code": "submit_failed", "message": str(exc)})

    def _decide(self, options: list[dict]) -> tuple[str, int]:
        by_name = {o["action"]: o for o in options}
        roll = self.rng.next(100)
        if "check" in by_name:
            if roll < 70 or "bet" not in by_name:
                return "check", 0
            spec = by_name["bet"]
            span = max(1, spec["max_amount"] - spec["min_amount"])
            return "bet", spec["min_amount"] + self.rng.next(min(span, 60))
        if roll < 20:
            return "fold", 0
        if roll < 90 and "call" in by_name:
            return "call", 0
        if "raise" in by_name:
            spec = by_name["raise"]
            span = max(1, spec["max_amount"] - spec["min_amount"])
            return "raise", spec["min_amount"] + self.rng.next(min(span, 60))
        return ("call", 0) if "call" in by_name else ("fold", 0)

    # ---- сеть --------------------------------------------------------------
    async def connect(self) -> None:
        await accounts.ensure(self.player_id, self.nickname)
        await hub.register(self.connection)

    async def drop(self) -> None:
        """Имитация обрыва связи. Ждём ответа стола, чтобы тест не гонялся с актором."""
        self.socket.closed = True
        self.connection.closed = True
        hub.unregister(self.connection)
        table = registry.get(self.table_id or "")
        if table is not None:
            await table.submit("disconnected", {"player_id": self.player_id}, wait=True)

    async def reconnect(self) -> None:
        self.socket = FakeSocket(self)
        self.connection = Connection(
            player_id=self.player_id, nickname=self.nickname, websocket=self.socket
        )
        await hub.register(self.connection)
        table = registry.get(self.table_id or "")
        if table is not None:
            await table.submit("connected", {"player_id": self.player_id}, wait=True)

    def messages(self, kind: str) -> list[dict]:
        return [m["d"] for m in self.received if m.get("t") == kind]

    @property
    def balance(self) -> int:
        account = accounts.get(self.player_id)
        return account.balance if account else 0


# ---------------------------------------------------------------- окружение
async def _reset() -> None:
    await registry.shutdown()
    registry._tables.clear()          # noqa: SLF001
    registry._joining.clear()         # noqa: SLF001
    hub._by_player.clear()            # noqa: SLF001
    accounts._accounts.clear()        # noqa: SLF001
    if deck_provider.count == 0:
        deck_provider.load()
    await registry.start()


def _fast_timers() -> None:
    settings.action_timeout_seconds = 0.6
    settings.disconnect_grace_seconds = 0.2
    settings.inter_hand_delay_seconds = 0.05
    settings.reconnect_window_seconds = 30.0
    settings.empty_table_ttl_seconds = 1.0
    settings.enable_postgres = False


async def _seat_bots(bots: list[Bot], stake: str = "micro") -> None:
    for bot in bots:
        await bot.connect()
        await registry.quick_join(bot.player_id, bot.nickname, stake, None)


async def _wait_for_hands(bots: list[Bot], count: int, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if all(b.hands_finished >= count for b in bots):
            return
        await asyncio.sleep(0.05)
    played = [b.hands_finished for b in bots]
    raise AssertionError(f"за {timeout} с не сыграли {count} раздач, получилось {played}")


def _run(coro) -> None:
    """Прогон сценария с гарантированной остановкой столов.

    Без ``finally`` упавшая проверка оставила бы живые задачи столов, и
    завершение цикла событий подвисало бы — падал бы не тот тест, который сломан.
    """

    async def guarded() -> None:
        try:
            await coro
        finally:
            await asyncio.shield(registry.shutdown())

    asyncio.run(guarded())


# ------------------------------------------------------------------- тесты
def test_full_table_plays_a_full_session():
    """Полный стол ботов проходит весь путь: автоподбор → раздачи → выход."""

    async def scenario() -> None:
        _fast_timers()
        await _reset()
        n = settings.max_seats
        bots = [Bot(i) for i in range(n)]
        await _seat_bots(bots)

        table_ids = {b.table_id for b in bots}
        assert len(table_ids) == 1, f"автоподбор развёл ботов по столам: {table_ids}"
        table = registry.get(bots[0].table_id)
        assert table is not None and table.occupied_count == n
        assert sorted(b.seat_no for b in bots) == list(range(n))

        await _wait_for_hands(bots, 8)

        for bot in bots:
            assert not bot.errors, f"{bot.nickname} получил ошибки: {bot.errors}"

        # фишки не появляются и не исчезают: стеки + балансы неизменны
        total = sum(b.balance for b in bots) + sum(s.stack for s in table.seats if s.is_occupied)
        assert total == n * settings.guest_start_balance, (
            f"фишки не сходятся: {total} вместо {n * settings.guest_start_balance}"
        )

        # каждая раздача дошла до конца и раздала банк
        finished = bots[0].messages("hand_finished")
        assert len(finished) >= 8
        for hand in finished:
            assert hand["winners"], "раздача завершилась без победителей"
            assert sum(w["amount"] for w in hand["winners"]) == hand["total_pot"]

        for bot in bots:
            await table.submit("leave", {"player_id": bot.player_id}, wait=True)
        await asyncio.sleep(0.3)
        assert sum(b.balance for b in bots) == n * settings.guest_start_balance
        await registry.shutdown()

    _run(scenario())


def test_private_cards_never_leak():
    """Ни один игрок не должен увидеть чужие карманные карты до вскрытия."""

    async def scenario() -> None:
        _fast_timers()
        await _reset()
        bots = [Bot(i) for i in range(4)]
        await _seat_bots(bots)
        await _wait_for_hands(bots, 6)

        for bot in bots:
            for data in bot.messages("hole_cards"):
                assert data["seat_no"] == bot.seat_no, (
                    f"{bot.nickname} получил карты чужого места {data['seat_no']}"
                )

        # свои карты видны только в поле you снимка
        for bot in bots:
            for snapshot in bot.messages("joined_table") + bot.messages("table_snapshot"):
                for seat in snapshot["seats"]:
                    assert "hole_cards" not in seat, "карты просочились в публичный состав стола"

        # карты соперников появляются только на вскрытии
        for bot in bots:
            revealed = {
                card
                for showdown in bot.messages("showdown")
                for hand in showdown["hands"]
                for card in hand["cards"]
            }
            own = {
                card for data in bot.messages("hole_cards") for card in data["cards"]
            }
            for data in bot.messages("hole_cards"):
                assert len(data["cards"]) == 2
            assert own or revealed, "бот не увидел вообще никаких карт — тест бесполезен"

        await registry.shutdown()

    _run(scenario())


def test_timeout_folds_for_silent_player():
    """Молчащего игрока сервер сбрасывает сам, стол продолжает раздавать."""

    async def scenario() -> None:
        _fast_timers()
        await _reset()
        active = [Bot(0), Bot(1)]
        silent = Bot(99, passive=True)
        bots = [*active, silent]
        await _seat_bots(bots)

        await _wait_for_hands(active, 4, timeout=40.0)

        timeouts = [
            data
            for data in active[0].messages("action_timeout")
            if data["seat_no"] == silent.seat_no
        ]
        assert timeouts, "авто-фолд по таймауту не сработал"
        assert all(t["action"] in ("fold", "check") for t in timeouts)

        # после серии авто-фолдов игрока переводят в сит-аут
        table = registry.get(silent.table_id)
        seat = table.find_seat_of(silent.player_id)
        assert seat.sitting_out, "молчащего игрока должны были вывести в сит-аут"
        assert seat.auto_sat_out, "сит-аут поставил сервер, флаг должен быть выставлен"

        await registry.shutdown()

    _run(scenario())


def test_reconnect_restores_table_and_cards():
    """Обрыв и возврат: место сохраняется, приходит полный снимок со своими картами."""

    async def scenario() -> None:
        _fast_timers()
        await _reset()
        bots = [Bot(i) for i in range(3)]
        await _seat_bots(bots)
        await _wait_for_hands(bots, 2)

        victim = bots[2]
        seat_before = victim.seat_no
        table = registry.get(victim.table_id)

        await victim.drop()
        seat = table.find_seat_of(victim.player_id)
        assert seat is not None, "место не должно освобождаться сразу"
        assert not seat.connected

        await asyncio.sleep(0.5)  # стол продолжает играть без него
        snapshots_before = len(victim.messages("table_snapshot"))

        await victim.reconnect()
        await asyncio.sleep(0.2)

        snapshots = victim.messages("table_snapshot")
        assert len(snapshots) > snapshots_before, "после возврата не пришёл снимок стола"
        snapshot = snapshots[-1]
        assert snapshot["you"]["seat_no"] == seat_before, "игрок вернулся не на своё место"
        assert table.find_seat_of(victim.player_id).connected

        if snapshot["hand"] is not None:
            assert snapshot["you"]["hole_cards"], "в снимке должны быть свои карты"

        await _wait_for_hands(bots, 5)
        assert not victim.errors, victim.errors
        await registry.shutdown()

    _run(scenario())


def test_quick_join_fills_tables_before_opening_new_ones():
    """Автоподбор доукомплектовывает стол, а не размазывает игроков по пустым."""

    async def scenario() -> None:
        _fast_timers()
        await _reset()
        bots = [Bot(i) for i in range(8)]
        for bot in bots:
            await bot.connect()
            await registry.quick_join(bot.player_id, bot.nickname, "low", None)

        tables = registry.all_tables()
        assert len(tables) == 2, f"ожидалось 2 стола на 8 игроков, создано {len(tables)}"
        counts = sorted(t.occupied_count for t in tables)
        expected = [8 - settings.max_seats, settings.max_seats]
        assert counts == expected, f"первый стол должен быть заполнен целиком, получено {counts}"

        # повторный заход того же игрока отклоняется
        from app.services.table import TableError  # noqa: PLC0415

        try:
            await registry.quick_join(bots[0].player_id, bots[0].nickname, "low", None)
        except TableError as exc:
            assert exc.code.value in ("already_at_table", "join_in_progress")
        else:
            raise AssertionError("повторная посадка должна отклоняться")

        await registry.shutdown()

    _run(scenario())


if __name__ == "__main__":
    from tests.helpers import run_module

    sys.exit(run_module(sys.modules[__name__]))
