"""Доменные правила, не зависящие от БД и внешних API."""

from src.domain.inn import is_valid_inn

__all__ = ["is_valid_inn"]
