"""Конфигурация приложения. Все значения переопределяются переменными окружения."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class StakeLevel(BaseModel):
    """Уровень ставок. Игрок выбирает уровень, автоподбор ищет стол внутри него.

    Именно ``BaseModel``, а не ``BaseSettings``: это значение из конфига, а не
    самостоятельный источник настроек. Иначе pydantic-settings пытался бы
    вычитывать поля ``code``/``title``/``small_blind`` из переменных окружения.
    """

    code: str
    title: str
    small_blind: int
    big_blind: int
    min_buy_in: int
    max_buy_in: int

    @property
    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "title": self.title,
            "small_blind": self.small_blind,
            "big_blind": self.big_blind,
            "min_buy_in": self.min_buy_in,
            "max_buy_in": self.max_buy_in,
        }


DEFAULT_STAKES: list[StakeLevel] = [
    StakeLevel(
        code="micro", title="Микро", small_blind=1, big_blind=2,
        min_buy_in=80, max_buy_in=200,
    ),
    StakeLevel(
        code="low", title="Низкие", small_blind=5, big_blind=10,
        min_buy_in=400, max_buy_in=1000,
    ),
    StakeLevel(
        code="mid", title="Средние", small_blind=25, big_blind=50,
        min_buy_in=2000, max_buy_in=5000,
    ),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- приложение ---------------------------------------------------------
    app_name: str = "neon-poker-backend"
    environment: str = "dev"
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    # --- авторизация (пока заглушка: гостевой JWT) --------------------------
    jwt_secret: str = "dev-only-secret-please-change-in-production"
    jwt_algorithm: str = "HS256"
    #: гостевой токен живёт долго намеренно: он же служит «ключом от аккаунта».
    #: Протухший гостевой токен = потерянный баланс, вернуть его нечем.
    #: С появлением настоящих аккаунтов здесь появится короткий access + refresh.
    guest_token_ttl_seconds: int = 60 * 60 * 24 * 30

    # --- хранилища ----------------------------------------------------------
    postgres_dsn: str = "postgresql+asyncpg://poker:poker@localhost:5432/poker"
    redis_url: str = "redis://localhost:6379/0"
    #: если Redis недоступен, продолжать на локальном счётчике колод
    allow_redis_fallback: bool = True
    #: работать без Postgres (профили игроков только в памяти) — удобно для локального прогона
    enable_postgres: bool = True

    # --- колоды -------------------------------------------------------------
    decks_file: Path = BASE_DIR / "data" / "decks.json"
    #: ключ глобального счётчика раздач, по нему выбирается очередная колода
    deck_cursor_key: str = "poker:deck:cursor"
    #: сжигать карту перед флопом/тёрном/ривером, как в живой игре
    burn_cards: bool = True

    # --- стол ---------------------------------------------------------------
    max_seats: int = 5
    min_players_to_start: int = 2
    #: секунд на решение игрока
    action_timeout_seconds: float = 20.0
    #: сколько добавляется к таймеру, если игрок в дисконнекте (даём шанс вернуться)
    disconnect_grace_seconds: float = 10.0
    #: пауза между раздачами, чтобы клиент успел проиграть анимацию
    inter_hand_delay_seconds: float = 4.0
    #: пауза между улицами
    street_delay_seconds: float = 1.0
    #: после скольких авто-фолдов подряд игрока переводят в sit-out
    auto_fold_limit: int = 3
    #: сколько секунд стол живёт пустым, прежде чем будет удалён
    empty_table_ttl_seconds: float = 60.0
    #: сколько секунд место держится за отключившимся игроком
    reconnect_window_seconds: float = 120.0
    #: писать историю раздач в Postgres (в MVP выключено, включается одним флагом)
    persist_hand_history: bool = False

    # --- лобби --------------------------------------------------------------
    stakes: list[StakeLevel] = Field(default_factory=lambda: list(DEFAULT_STAKES))
    #: стартовый баланс гостя
    guest_start_balance: int = 10_000

    def stake(self, code: str) -> StakeLevel | None:
        return next((s for s in self.stakes if s.code == code), None)

    @property
    def default_stake(self) -> StakeLevel:
        return self.stakes[0]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
