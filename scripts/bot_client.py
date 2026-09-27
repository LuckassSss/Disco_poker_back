#!/usr/bin/env python3
"""Боты садятся за стол и играют — быстрый способ увидеть, что сервер жив.

Ботам не нужен Unity: они делают ровно то же, что будет делать клиент —
берут гостевой токен по HTTP, открывают WebSocket, шлют ``quick_join`` и
отвечают на ``action_required``.

Зависимостей нет: работает на голом ``python3`` (3.11+), ставить ничего не надо.

Запуск (сервер должен быть поднят)::

    python3 scripts/bot_client.py --bots 5 --hands 20 --stake low

Пять ботов, а не шесть — тогда за столом останется свободное место
для вашего клиента.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ws_client import WebSocketError, connect  # noqa: E402


class Lcg:
    """Детерминированный источник решений — прогон всегда воспроизводится."""

    def __init__(self, seed: int) -> None:
        self.state = seed

    def next(self, bound: int) -> int:
        self.state = (self.state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return (self.state >> 33) % bound


def get_token(base_url: str, nickname: str) -> str:
    request = urllib.request.Request(
        f"{base_url}/auth/guest",
        data=json.dumps({"nickname": nickname}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)["token"]
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Не достучался до сервера на {base_url}: {exc.reason}.\n"
            "Сервер запущен? Проверьте: curl " + base_url + "/health"
        ) from None


async def bot(index: int, base_url: str, ws_url: str, stake: str, hands: int, quiet: bool) -> None:
    nickname = f"Бот-{index}"
    # urllib синхронный: уводим в поток, чтобы не блокировать цикл событий
    # (иначе боты авторизуются строго по очереди, а в тестах это ещё и дедлок)
    token = await asyncio.to_thread(get_token, base_url, nickname)
    rng = Lcg(1000 + index)
    finished = 0
    seat_no = -1
    min_buy_in = max_buy_in = 0

    async with await connect(f"{ws_url}/ws?token={token}") as ws:
        await ws.send(json.dumps({"t": "quick_join", "d": {"stake": stake}}))

        async for raw in ws:
            message = json.loads(raw)
            kind, data = message.get("t"), message.get("d", {})

            if kind == "joined_table":
                seat_no = data["you"]["seat_no"]
                min_buy_in = data["table"]["stake"]["min_buy_in"]
                max_buy_in = data["table"]["stake"]["max_buy_in"]
                if not quiet:
                    print(f"[{nickname}] сел за {data['table']['table_id']}, место {seat_no}")

            elif kind == "action_required" and data.get("options"):
                await asyncio.sleep(0.15 + rng.next(40) / 100)
                options = {o["action"]: o for o in data["options"]}
                choice = _decide(options, rng)
                await ws.send(
                    json.dumps(
                        {
                            "t": "action",
                            "d": {
                                "hand_id": data.get("hand_id"),
                                "action": choice[0],
                                "amount": choice[1],
                            },
                        }
                    )
                )

            elif kind == "hand_finished":
                finished += 1
                # стек ниже минимального бай-ина — доливаем до максимума, иначе сервер посадит бота в сит-аут
                mine = next((x for x in data.get("seats", []) if x["seat_no"] == seat_no), None)
                if mine and mine["stack_after"] < min_buy_in:
                    top_up = max_buy_in - mine["stack_after"]
                    await ws.send(json.dumps({"t": "rebuy", "d": {"amount": top_up}}))
                    if not quiet:
                        print(f"[{nickname}] докупка +{top_up}")
                if index == 0 and not quiet:
                    winners = ", ".join(
                        f"место {w['seat_no']} +{w['amount']}"
                        + (f" ({w['combination']})" if w.get("combination") else "")
                        for w in data["winners"]
                    )
                    board = " ".join(data["board"]) or "—"
                    print(f"  раздача {finished}: борд [{board}] банк {data['total_pot']} → {winners}")
                if finished >= hands:
                    await ws.send(json.dumps({"t": "leave_table", "d": {}}))
                    return

            elif kind == "error":
                print(f"[{nickname}] ошибка: {data.get('code')} — {data.get('message')}")


def _decide(options: dict, rng: Lcg) -> tuple[str, int]:
    roll = rng.next(100)
    if "check" in options:
        if roll < 65:
            return "check", 0
        if "bet" in options:
            spec = options["bet"]
            span = max(1, spec["max_amount"] - spec["min_amount"])
            return "bet", spec["min_amount"] + rng.next(min(span, spec["min_amount"] * 3 or span))
        return "check", 0
    if roll < 20:
        return "fold", 0
    if roll < 88 and "call" in options:
        return "call", 0
    if "raise" in options:
        spec = options["raise"]
        span = max(1, spec["max_amount"] - spec["min_amount"])
        return "raise", spec["min_amount"] + rng.next(min(span, spec["min_amount"] * 2 or span))
    return "call" if "call" in options else "fold", 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost:8000")
    parser.add_argument("--bots", type=int, default=5)
    parser.add_argument("--hands", type=int, default=10)
    parser.add_argument("--stake", default="low")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    base_url = f"http://{args.host}"
    ws_url = f"ws://{args.host}"
    print(f"Запускаю {args.bots} ботов на {base_url}, уровень ставок {args.stake}…")

    results = await asyncio.gather(
        *(
            bot(i, base_url, ws_url, args.stake, args.hands, args.quiet)
            for i in range(args.bots)
        ),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    for failure in failures:
        print(f"Бот упал: {type(failure).__name__}: {failure}", file=sys.stderr)
    print("Готово." if not failures else f"Готово, но {len(failures)} ботов отвалились.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except WebSocketError as exc:
        raise SystemExit(f"Ошибка соединения: {exc}") from None
