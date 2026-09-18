"""Тесты детерминированной выдачи колод."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.cards import FULL_DECK, validate_deck  # noqa: E402
from app.core.deck_provider import DeckProvider  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DECKS_FILE = ROOT / "data" / "decks.json"


def _provider() -> DeckProvider:
    return DeckProvider(DECKS_FILE).load()


def test_decks_file_is_valid():
    raw = json.loads(DECKS_FILE.read_text(encoding="utf-8"))
    assert raw["count"] == len(raw["decks"]) >= 10
    for deck in raw["decks"]:
        cards = validate_deck(deck["cards"])
        assert sorted(cards) == sorted(FULL_DECK)


def test_decks_are_distinct():
    """10 колод должны отличаться друг от друга — иначе смысла в них нет."""
    provider = _provider()
    signatures = {d.sha256 for d in provider.decks}
    assert len(signatures) == provider.count


def test_generation_is_reproducible():
    """Перегенерация тем же сидом даёт байт в байт тот же результат."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from scripts.generate_decks import shuffle_deck  # noqa: PLC0415

    raw = json.loads(DECKS_FILE.read_text(encoding="utf-8"))
    for deck in raw["decks"]:
        assert shuffle_deck(raw["seed_prefix"], deck["id"]) == deck["cards"]


def test_sequence_cycles_through_all_decks():
    """Колоды берутся друг за другом по кругу, без пропусков и повторов внутри круга."""
    provider = _provider()

    async def go() -> list[int]:
        return [(await provider.next_deck()).deck_id for _ in range(provider.count * 2)]

    ids = asyncio.run(go())
    first_round = ids[: provider.count]
    second_round = ids[provider.count :]
    assert first_round == list(range(provider.count))
    assert second_round == first_round, "после круга последовательность повторяется"


def test_sequence_number_grows():
    provider = _provider()

    async def go() -> list[int]:
        return [(await provider.next_deck()).sequence for _ in range(5)]

    assert asyncio.run(go()) == [1, 2, 3, 4, 5]


def test_peek_matches_actual_deal():
    provider = _provider()

    async def go() -> None:
        for _ in range(15):
            dealt = await provider.next_deck()
            assert provider.peek(dealt.sequence).cards == dealt.cards

    asyncio.run(go())


def test_cursor_draws_without_repeats():
    provider = _provider()

    async def go() -> None:
        cursor = await provider.next_cursor()
        drawn: list[str] = []
        for _ in range(10):
            drawn += cursor.draw(5)
        assert len(set(drawn)) == 50
        assert cursor.remaining == 2

    asyncio.run(go())


def test_cursor_refuses_to_overdraw():
    provider = _provider()

    async def go() -> None:
        cursor = await provider.next_cursor()
        cursor.draw(52)
        try:
            cursor.draw(1)
        except RuntimeError:
            return
        raise AssertionError("курсор обязан ругаться при выходе за пределы колоды")

    asyncio.run(go())


def test_broken_deck_is_rejected():
    """Битый файл колод должен падать на загрузке, а не в середине раздачи."""
    for bad in ([], ["As"] * 52, list(FULL_DECK)[:51]):
        try:
            validate_deck(bad)
        except ValueError:
            continue
        raise AssertionError(f"должно было упасть: {bad[:3]}")


if __name__ == "__main__":
    from tests.helpers import run_module

    sys.exit(run_module(sys.modules[__name__]))
