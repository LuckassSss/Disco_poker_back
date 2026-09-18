"""Тесты оценщика комбинаций."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.enums import HandCategory  # noqa: E402
from app.core.hand_eval import evaluate  # noqa: E402


def cat(*cards: str) -> HandCategory:
    return evaluate(list(cards)).category


def test_categories_are_recognised():
    assert cat("As", "Ks", "Qs", "Js", "Ts", "2c", "3d") is HandCategory.STRAIGHT_FLUSH
    assert cat("9h", "9d", "9s", "9c", "2c", "3d", "4h") is HandCategory.QUADS
    assert cat("9h", "9d", "9s", "2c", "2d", "5h", "7s") is HandCategory.FULL_HOUSE
    assert cat("Ah", "Jh", "9h", "5h", "2h", "3c", "4d") is HandCategory.FLUSH
    assert cat("9h", "8d", "7s", "6c", "5h", "2d", "3c") is HandCategory.STRAIGHT
    assert cat("9h", "9d", "9s", "2c", "5d", "7h", "Jc") is HandCategory.TRIPS
    assert cat("9h", "9d", "5s", "5c", "2d", "7h", "Jc") is HandCategory.TWO_PAIR
    assert cat("9h", "9d", "Ks", "5c", "2d", "7h", "Jc") is HandCategory.PAIR
    assert cat("9h", "Td", "Ks", "5c", "2d", "7h", "Jc") is HandCategory.HIGH_CARD


def test_wheel_straight():
    """A-2-3-4-5 — это стрит до пятёрки, а не до туза."""
    rank = evaluate(["Ah", "2d", "3s", "4c", "5h", "Kd", "Qc"])
    assert rank.category is HandCategory.STRAIGHT
    assert rank.key[1] == 5, "старшая карта колеса — пятёрка"
    # обычный стрит до шестёрки должен быть сильнее колеса
    better = evaluate(["2d", "3s", "4c", "5h", "6h", "Kd", "Qc"])
    assert better > rank


def test_wheel_straight_flush():
    rank = evaluate(["Ah", "2h", "3h", "4h", "5h", "Kd", "Qc"])
    assert rank.category is HandCategory.STRAIGHT_FLUSH
    assert rank.key[1] == 5


def test_flush_beats_straight():
    straight = evaluate(["9h", "8d", "7s", "6c", "5h", "2d", "3c"])
    flush = evaluate(["Ah", "Jh", "9h", "5h", "2h", "3c", "4d"])
    assert flush > straight


def test_kicker_decides():
    a = evaluate(["Kh", "Kd", "Ah", "7c", "4d", "3s", "2c"])
    b = evaluate(["Ks", "Kc", "Qh", "7d", "4h", "3c", "2d"])
    assert a > b, "пара королей с тузовым кикером сильнее"


def test_identical_hands_are_equal():
    """Оба игрока играют борд — должен быть сплит."""
    board = ["As", "Ks", "Qs", "Js", "9d"]
    a = evaluate(board + ["2c", "3d"])
    b = evaluate(board + ["4h", "5h"])
    assert a.key == b.key


def test_equality_ignores_which_cards_were_used():
    """Регрессия: сравнение на равенство идёт по силе, а не по конкретным картам.

    Оба собрали стрит до валета, но каждый — из своей десятки. Руки равны,
    иначе сплит-поты бы не работали.
    """
    board = ["Jc", "9d", "8h", "7h", "2c"]
    a = evaluate(board + ["Td", "As"])
    b = evaluate(board + ["Tc", "Ks"])
    assert a.best_five != b.best_five, "наборы карт действительно разные"
    assert a == b, "но по силе руки равны"
    assert not (a > b) and not (b > a)
    assert len({a, b}) == 1, "равные руки должны совпадать и как ключи множества"


def test_equality_on_board_play():
    board = ["As", "Kd", "Qc", "Jh", "Ts"]
    assert evaluate(board + ["Ah", "2c"]) == evaluate(board + ["2h", "3s"])


def test_equality_on_quads_on_board():
    board = ["2s", "2d", "2c", "2h", "Kd"]
    assert evaluate(board + ["Ah", "Qc"]) == evaluate(board + ["As", "Jd"])


def test_two_pair_kicker():
    a = evaluate(["9h", "9d", "5s", "5c", "Ad", "7h", "2c"])
    b = evaluate(["9s", "9c", "5h", "5d", "Kd", "7s", "2h"])
    assert a > b


def test_full_house_over_flush():
    fh = evaluate(["9h", "9d", "9s", "2c", "2d", "5h", "7s"])
    fl = evaluate(["Ah", "Jh", "9h", "5h", "2h", "3c", "4d"])
    assert fh > fl


def test_best_five_is_five_cards():
    for cards in (
        ["As", "Ks", "Qs", "Js", "Ts", "2c", "3d"],
        ["9h", "9d", "9s", "9c", "2c", "3d", "4h"],
        ["9h", "Td", "Ks", "5c", "2d", "7h", "Jc"],
        ["Ah", "2d", "3s", "4c", "5h", "Kd", "Qc"],
        ["9h", "9d", "5s", "5c", "2d", "7h", "Jc"],
    ):
        rank = evaluate(cards)
        assert len(rank.best_five) == 5, f"{cards} -> {rank.best_five}"
        assert len(set(rank.best_five)) == 5, f"дубликаты в {rank.best_five}"
        assert set(rank.best_five) <= set(cards)


def test_trips_over_two_pair_on_paired_board():
    board = ["7h", "7d", "Kc", "4s", "9h"]
    trips = evaluate(board + ["7s", "2c"])
    two_pair = evaluate(board + ["Kd", "4h"])
    assert trips.category is HandCategory.TRIPS
    assert two_pair.category is HandCategory.TWO_PAIR
    assert trips > two_pair


def test_six_card_input_supported():
    rank = evaluate(["As", "Ks", "Qs", "Js", "Ts", "2c"])
    assert rank.category is HandCategory.STRAIGHT_FLUSH


if __name__ == "__main__":
    from tests.helpers import run_module

    sys.exit(run_module(sys.modules[__name__]))
