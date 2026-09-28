"""Тесты машины состояний раздачи: очередь хода, правила ставок, вскрытие."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.enums import ActionType, SeatState, Street  # noqa: E402
from app.core.game import HandEngine, InvalidAction  # noqa: E402
from tests.helpers import make_engine, stacked_deck  # noqa: E402


def act(engine: HandEngine, action: ActionType, amount: int = 0) -> None:
    assert engine.to_act is not None, "ожидался чей-то ход, а раздача уже не ждёт действий"
    engine.act(engine.to_act, action, amount)


def events_of(engine: HandEngine, type_: str) -> list[dict]:
    return [e.data for e in engine.events if e.type == type_]


# --------------------------------------------------------------- порядок хода
def test_six_max_preflop_order():
    """6-max: баттон 0 → SB место 1, BB место 2, первым ходит место 3."""
    engine, seats = make_engine([1000] * 6, stacked_deck(), button=0)
    engine.start()
    started = events_of(engine, "hand_started")[0]
    assert started["small_blind_seat"] == 1
    assert started["big_blind_seat"] == 2
    assert engine.to_act == 3
    assert seats[1].bet == 5 and seats[2].bet == 10
    assert engine.pot_total == 15


def test_heads_up_button_is_small_blind():
    """В хедз-апе баттон ставит малый блайнд и ходит первым до флопа."""
    engine, seats = make_engine([1000, 1000], stacked_deck(), button=0)
    engine.start()
    started = events_of(engine, "hand_started")[0]
    assert started["small_blind_seat"] == 0
    assert started["big_blind_seat"] == 1
    assert engine.to_act == 0


def test_heads_up_postflop_order_flips():
    """После флопа в хедз-апе первым ходит большой блайнд."""
    engine, _ = make_engine([1000, 1000], stacked_deck(), button=0)
    engine.start()
    act(engine, ActionType.CALL)   # баттон коллирует
    act(engine, ActionType.CHECK)  # BB чекает → флоп
    assert engine.street is Street.FLOP
    assert engine.to_act == 1, "постфлоп первым ходит BB"


def test_postflop_order_starts_left_of_button():
    engine, _ = make_engine([1000] * 6, stacked_deck(), button=0)
    engine.start()
    for _ in range(4):
        act(engine, ActionType.CALL)  # места 3,4,5,0
    act(engine, ActionType.CALL)      # SB добирает
    act(engine, ActionType.CHECK)     # BB использует опцию
    assert engine.street is Street.FLOP
    assert engine.to_act == 1, "первым на флопе ходит первый живой слева от баттона"


def test_big_blind_gets_option_after_limps():
    """Все уравняли — большой блайнд обязан получить право хода."""
    engine, _ = make_engine([1000] * 3, stacked_deck(), button=0)
    engine.start()
    assert engine.to_act == 0
    act(engine, ActionType.CALL)  # баттон
    act(engine, ActionType.CALL)  # SB
    assert engine.to_act == 2, "у BB должна остаться опция"
    assert engine.street is Street.PREFLOP


# --------------------------------------------------------------- правила ставок
def test_raise_below_minimum_rejected():
    engine, _ = make_engine([1000] * 3, stacked_deck(), button=0)
    engine.start()
    try:
        engine.act(engine.to_act, ActionType.RAISE, 15)  # минимум 20
    except InvalidAction:
        return
    raise AssertionError("рейз меньше минимального обязан отклоняться")


def test_min_raise_grows_after_reraise():
    engine, _ = make_engine([1000] * 3, stacked_deck(), button=0)
    engine.start()
    act(engine, ActionType.RAISE, 30)   # приращение 20 → новый мин. рейз 20
    assert engine.min_raise == 20
    act(engine, ActionType.RAISE, 80)   # приращение 50
    assert engine.min_raise == 50
    options = {a.action: a for a in engine.legal_actions(engine.to_act)}
    assert options[ActionType.RAISE].min_amount == 130


def test_short_all_in_does_not_reopen_betting():
    """Неполный олл-ин не даёт уже сходившему игроку право на ре-рейз."""
    #                баттон 0    SB 1      BB 2
    engine, seats = make_engine([1000, 1000, 55], stacked_deck(), button=0)
    engine.start()
    act(engine, ActionType.RAISE, 40)   # место 0 рейзит до 40
    act(engine, ActionType.FOLD)        # место 1 пас
    # у BB всего 55 → олл-ин до 55, приращение 15 < мин. рейза 20
    act(engine, ActionType.ALL_IN)
    assert seats[2].state is SeatState.ALL_IN
    assert engine.to_act == 0
    options = {a.action for a in engine.legal_actions(0)}
    assert ActionType.RAISE not in options, "ре-рейз после неполного олл-ина недоступен"
    assert ActionType.CALL in options


def test_full_all_in_reopens_betting():
    """Полноценный олл-ин-рейз возвращает право на ре-рейз тем, кто уже ходил."""
    #                 баттон 0    SB 1      BB 2 (коротышка)
    engine, _ = make_engine([1000, 1000, 200], stacked_deck(), button=0)
    engine.start()
    act(engine, ActionType.RAISE, 40)  # место 0
    act(engine, ActionType.CALL)       # место 1 остаётся в игре со стеком
    act(engine, ActionType.ALL_IN)     # место 2 до 200 — приращение 160, полный рейз
    options = {a.action for a in engine.legal_actions(0)}
    assert ActionType.RAISE in options


def test_no_raise_when_only_all_in_opponent_left():
    """Повышать некому: единственный соперник в олл-ине — остаётся колл или пас."""
    engine, _ = make_engine([1000, 1000, 200], stacked_deck(), button=0)
    engine.start()
    act(engine, ActionType.RAISE, 40)
    act(engine, ActionType.FOLD)     # место 1 выходит
    act(engine, ActionType.ALL_IN)   # место 2 в олл-ине на 200
    options = {a.action for a in engine.legal_actions(0)}
    assert options == {ActionType.FOLD, ActionType.CALL}


def test_short_all_in_does_not_reopen_via_all_in_button():
    """Обход через кнопку «олл-ин» тоже закрыт: неполный олл-ин не открывает торговлю.

    Места: 0 — баттон 1000, 1 — SB 55, 2 — BB 1000, 3 — UTG 1000.
    """
    engine, _ = make_engine([1000, 55, 1000, 1000], stacked_deck(), button=0)
    engine.start()
    assert engine.to_act == 3
    act(engine, ActionType.RAISE, 40)  # место 3, приращение 30
    act(engine, ActionType.CALL)       # место 0
    act(engine, ActionType.ALL_IN)     # место 1 до 55 — приращение 15 < 30
    act(engine, ActionType.CALL)       # место 2 добирает до 55
    assert engine.to_act == 3
    options = {a.action for a in engine.legal_actions(3)}
    assert ActionType.RAISE not in options
    assert ActionType.ALL_IN not in options, "олл-ин не должен обходить запрет на ре-рейз"
    assert options == {ActionType.FOLD, ActionType.CALL}


def test_check_not_allowed_facing_bet():
    engine, _ = make_engine([1000] * 3, stacked_deck(), button=0)
    engine.start()
    options = {a.action for a in engine.legal_actions(engine.to_act)}
    assert ActionType.CHECK not in options
    assert ActionType.CALL in options


# --------------------------------------------------------------- завершение раздачи
def test_everyone_folds_blind_wins_and_gets_change():
    engine, seats = make_engine([1000, 1000, 1000], stacked_deck(), button=0)
    engine.start()
    act(engine, ActionType.FOLD)  # баттон
    act(engine, ActionType.FOLD)  # SB
    assert engine.finished
    assert seats[2].stack == 1005, "BB забирает блайнд малого"
    assert seats[1].stack == 995
    assert seats[0].stack == 1000
    result = engine.result
    assert result is not None and not result.went_to_showdown
    winner = events_of(engine, "hand_finished")[0]["winners"][0]
    assert winner["seat_no"] == 2
    assert winner["cards"] == seats[2].hole_cards, "карты победителя показываются всем"
    assert len(winner["cards"]) == 2


def test_showdown_awards_best_hand():
    """Хедз-ап: у места 0 сет тузов против двух пар — банк уходит месту 0."""
    deck = stacked_deck(
        "Ah", "Kd",   # первые карты: место 0 и место 1
        "Ac", "Qs",   # вторые карты
        "As", "Kc", "Qd",  # флоп
        "2h",              # тёрн
        "7s",              # ривер
    )
    engine, seats = make_engine([1000, 1000], deck, button=0)
    engine.start()
    act(engine, ActionType.CALL)
    act(engine, ActionType.CHECK)
    for _ in range(3):  # флоп, тёрн, ривер — чек-чек
        act(engine, ActionType.CHECK)
        act(engine, ActionType.CHECK)
    assert engine.finished
    result = engine.result
    assert result is not None and result.went_to_showdown
    winners = {a.seat_no for a in result.awards}
    assert winners == {0}, f"ожидался выигрыш места 0, получено {result.awards}"
    assert seats[0].stack == 1010 and seats[1].stack == 990


def test_split_pot_on_identical_hands():
    """Оба играют борд — банк делится пополам."""
    deck = stacked_deck(
        "2c", "2d",
        "3c", "3d",
        "As", "Ks", "Qs",
        "Js",
        "Ts",
    )
    engine, seats = make_engine([1000, 1000], deck, button=0)
    engine.start()
    act(engine, ActionType.CALL)
    act(engine, ActionType.CHECK)
    for _ in range(3):
        act(engine, ActionType.CHECK)
        act(engine, ActionType.CHECK)
    assert seats[0].stack == 1000 and seats[1].stack == 1000


def test_split_pot_when_best_five_differs_by_suit():
    """Регрессия: равные по силе руки из РАЗНЫХ карт обязаны делить банк.

    У обоих стрит до валета, но пятёрки собраны из своих десяток —
    наборы карт разные, сила одинаковая. Банк должен делиться пополам.
    """
    deck = stacked_deck(
        "Td", "Tc",   # первые карты: место 0 и место 1
        "As", "Ks",   # вторые
        "Jc", "9d", "8h",  # флоп
        "7h",              # тёрн
        "2c",              # ривер
    )
    engine, seats = make_engine([1000, 1000], deck, button=0)
    engine.start()
    act(engine, ActionType.CALL)
    act(engine, ActionType.CHECK)
    for _ in range(3):
        act(engine, ActionType.CHECK)
        act(engine, ActionType.CHECK)
    assert seats[0].stack == 1000 and seats[1].stack == 1000, (
        f"ожидался сплит, получено {seats[0].stack}/{seats[1].stack}"
    )


def test_chips_survive_when_side_pot_contributors_all_fold():
    """Регрессия: слой банка, который сложили только спасовавшие, не должен пропадать.

    Место 1 в олл-ине на одну фишку, места 0 и 2 сбрасывают — их вклады
    образуют слой без претендентов. Эти фишки обязаны достаться месту 1.
    """
    # баттон 1 → SB место 2, BB место 0
    engine, seats = make_engine([15, 1, 6], stacked_deck(), button=1)
    total_before = 22
    engine.start()
    assert engine.to_act == 1
    act(engine, ActionType.CALL)   # место 1 идёт олл-ин на 1 фишку
    act(engine, ActionType.FOLD)   # место 2
    act(engine, ActionType.FOLD)   # место 0 пасует, хотя мог чекнуть
    assert engine.finished
    assert sum(s.stack for s in seats) == total_before, (
        f"фишки потерялись: {[s.stack for s in seats]}"
    )


def test_short_blind_all_in_heads_up_does_not_get_a_turn():
    """Регрессия: блайнд, ушедший в олл-ин при постановке, не должен получать ход.

    Иначе по таймауту сервер «сбрасывал» карты за игрока, который уже всё поставил.
    """
    engine, seats = make_engine([3, 1000], stacked_deck(), button=0)
    engine.start()
    assert seats[0].state is SeatState.ALL_IN
    assert engine.to_act == 1, "ход должен перейти большому блайнду"
    options = {a.action for a in engine.legal_actions(1)}
    assert ActionType.CHECK in options
    assert ActionType.RAISE not in options, "повышать против олл-ина на 3 фишки некому"
    act(engine, ActionType.CHECK)
    assert engine.finished
    assert len(engine.board) == 5, "борд должен быть доведён до конца"
    assert sum(s.stack for s in seats) == 1003


def test_all_in_runs_out_the_board_without_actions():
    """Когда торговаться некому, оставшиеся улицы открываются автоматически."""
    engine, _ = make_engine([200, 200], stacked_deck(), button=0)
    engine.start()
    act(engine, ActionType.ALL_IN)
    act(engine, ActionType.CALL)
    assert engine.finished
    assert len(engine.board) == 5, "борд должен быть доведён до ривера"


def test_side_pot_in_real_hand():
    """Коротышка в олл-ине выигрывает основной банк, сайд-пот делят остальные."""
    #  места:   0 (баттон, 1000)  1 (SB, 60)  2 (BB, 1000)
    deck = stacked_deck(
        # раздача идёт с SB: место 1, место 2, место 0
        "Ah", "2c", "Kh",   # первые карты: s1=Ah, s2=2c, s0=Kh
        "Ad", "7d", "Kd",   # вторые:       s1=Ad, s2=7d, s0=Kd
        "As", "Kc", "9h",   # флоп
        "3s",               # тёрн
        "4c",               # ривер
    )
    engine, seats = make_engine([1000, 60, 1000], deck, button=0)
    engine.start()
    act(engine, ActionType.RAISE, 200)   # место 0
    act(engine, ActionType.ALL_IN)       # место 1 — всего 60
    act(engine, ActionType.CALL)         # место 2 коллирует 200
    # дальше играют места 0 и 2
    while not engine.finished:
        act(engine, ActionType.CHECK)

    result = engine.result
    assert result is not None
    by_seat: dict[int, int] = {}
    for a in result.awards:
        by_seat[a.seat_no] = by_seat.get(a.seat_no, 0) + a.amount
    # у места 1 сет тузов (Ah Ad + As) — сильнее сета королей у места 0
    assert by_seat.get(1) == 180, f"основной банк 60x3=180 коротышке, получено {by_seat}"
    assert by_seat.get(0) == 280, f"сайд-пот (200-60)x2 месту 0, получено {by_seat}"
    assert sum(s.stack for s in seats) == 2060, "фишки не должны появляться из воздуха"


# --------------------------------------------------------------- инварианты
def test_chips_are_conserved_in_scripted_hand():
    total_before = 1000 + 750 + 480 + 2000
    engine, seats = make_engine([1000, 750, 480, 2000], stacked_deck(), button=0)
    engine.start()
    script = [
        (ActionType.RAISE, 35),
        (ActionType.CALL, 0),
        (ActionType.CALL, 0),
        (ActionType.FOLD, 0),
    ]
    for action, amount in script:
        act(engine, action, amount)
    while not engine.finished:
        options = {a.action for a in engine.legal_actions(engine.to_act)}
        act(engine, ActionType.CHECK if ActionType.CHECK in options else ActionType.CALL)
    assert sum(s.stack for s in seats) == total_before


def test_no_duplicate_cards_dealt():
    engine, seats = make_engine([1000] * 6, stacked_deck(), button=0)
    engine.start()
    while not engine.finished:
        options = {a.action for a in engine.legal_actions(engine.to_act)}
        act(engine, ActionType.CHECK if ActionType.CHECK in options else ActionType.CALL)
    all_cards = list(engine.board)
    for seat in seats:
        all_cards += seat.hole_cards
    assert len(all_cards) == len(set(all_cards)), "одна и та же карта выдана дважды"


def test_folded_player_cannot_act():
    engine, _ = make_engine([1000] * 3, stacked_deck(), button=0)
    engine.start()
    folded = engine.to_act
    act(engine, ActionType.FOLD)
    try:
        engine.act(folded, ActionType.CALL, 0)
    except InvalidAction:
        return
    raise AssertionError("сбросивший карты не может ходить")


def test_acting_out_of_turn_rejected():
    engine, _ = make_engine([1000] * 3, stacked_deck(), button=0)
    engine.start()
    wrong = (engine.to_act + 1) % 3
    try:
        engine.act(wrong, ActionType.CALL, 0)
    except InvalidAction:
        return
    raise AssertionError("ход вне очереди обязан отклоняться")


if __name__ == "__main__":
    from tests.helpers import run_module

    sys.exit(run_module(sys.modules[__name__]))
