"""Выдача заранее замешанных колод.

Ключевое требование проекта: **в рантайме нет генерации случайных чисел**.
Колоды сгенерированы заранее скриптом ``scripts/generate_decks.py`` и лежат
в ``data/decks.json``. Сервер только берёт их по кругу.

Порядок выдачи — глобальная очередь: атомарный ``INCR`` в Redis на один общий
ключ для всех столов и всех инстансов сервера. Значит, две раздачи, начавшиеся
одна за другой на разных столах, гарантированно получат разные колоды, а после
10 раздач круг повторится.

Если Redis недоступен и разрешён фолбэк — используется локальный счётчик
процесса (для одиночного локального запуска этого достаточно, но для нескольких
инстансов уникальность уже не гарантируется, о чём пишем в лог).
"""
from __future__ import annotations

import itertools
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.core.cards import validate_deck
from app.infra.redis_client import get_redis

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DealtDeck:
    """Колода, выданная на конкретную раздачу."""

    #: индекс колоды в файле (0..count-1)
    deck_id: int
    #: сквозной номер выдачи — растёт бесконечно, удобен для аудита
    sequence: int
    cards: tuple[str, ...]
    sha256: str

    def iterator(self) -> "DeckCursor":
        return DeckCursor(self)


class DeckCursor:
    """Курсор по колоде: раздаёт карты строго по порядку, без возврата."""

    __slots__ = ("_deck", "_pos")

    def __init__(self, deck: DealtDeck) -> None:
        self._deck = deck
        self._pos = 0

    @property
    def deck_id(self) -> int:
        return self._deck.deck_id

    @property
    def sequence(self) -> int:
        return self._deck.sequence

    @property
    def remaining(self) -> int:
        return len(self._deck.cards) - self._pos

    def draw(self, n: int = 1) -> list[str]:
        if self.remaining < n:
            raise RuntimeError(
                f"В колоде осталось {self.remaining} карт, запрошено {n}"
            )
        chunk = list(self._deck.cards[self._pos : self._pos + n])
        self._pos += n
        return chunk

    def burn(self) -> str | None:
        """Сжигает карту перед новой улицей (если включено настройкой)."""
        if not settings.burn_cards:
            return None
        return self.draw(1)[0]


class DeckProvider:
    """Хранит пул колод и отдаёт очередную по глобальному счётчику."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or settings.decks_file
        self._decks: list[DealtDeck] = []
        self._local_counter = itertools.count(1)
        self._used_redis = False

    # ---- загрузка ----------------------------------------------------------
    def load(self) -> "DeckProvider":
        raw = json.loads(Path(self._path).read_text(encoding="utf-8"))
        decks = raw.get("decks") or []
        if not decks:
            raise ValueError(f"В файле {self._path} нет ни одной колоды")
        self._decks = [
            DealtDeck(
                deck_id=int(d["id"]),
                sequence=0,
                cards=tuple(validate_deck(d["cards"])),
                sha256=str(d.get("sha256", "")),
            )
            for d in decks
        ]
        log.info(
            "Загружено %s заранее замешанных колод из %s (алгоритм: %s)",
            len(self._decks),
            self._path,
            raw.get("algorithm", "n/a"),
        )
        return self

    @property
    def count(self) -> int:
        return len(self._decks)

    @property
    def decks(self) -> list[DealtDeck]:
        return list(self._decks)

    # ---- выдача ------------------------------------------------------------
    async def next_sequence(self) -> int:
        """Следующий номер раздачи. Атомарно и глобально, когда есть Redis."""
        redis = get_redis()
        if redis is not None:
            try:
                value = await redis.incr(settings.deck_cursor_key)
                self._used_redis = True
                return int(value)
            except Exception as exc:  # noqa: BLE001
                if not settings.allow_redis_fallback:
                    raise
                log.warning("INCR в Redis не удался (%s), беру локальный счётчик", exc)
        if self._used_redis:
            log.warning(
                "Redis пропал — счётчик колод стал локальным, "
                "при нескольких инстансах возможны совпадения колод"
            )
            self._used_redis = False
        return next(self._local_counter)

    async def next_deck(self) -> DealtDeck:
        if not self._decks:
            raise RuntimeError("DeckProvider не загружен: вызовите load()")
        seq = await self.next_sequence()
        deck = self._decks[(seq - 1) % len(self._decks)]
        return DealtDeck(
            deck_id=deck.deck_id,
            sequence=seq,
            cards=deck.cards,
            sha256=deck.sha256,
        )

    async def next_cursor(self) -> DeckCursor:
        return DeckCursor(await self.next_deck())

    def peek(self, sequence: int) -> DealtDeck:
        """Какая колода соответствует данному номеру раздачи (для тестов/аудита)."""
        return self._decks[(sequence - 1) % len(self._decks)]


deck_provider = DeckProvider()
