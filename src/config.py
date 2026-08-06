"""Загрузка и проверка настроек приложения."""

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def loadEnvironment() -> None:
    """Загрузить локальный файл .env из корня проекта."""
    load_dotenv(PROJECT_ROOT / ".env")


def requireSetting(name: str) -> str:
    """Получить обязательную настройку или завершить работу понятной ошибкой."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value

