"""Проверка российского ИНН по длине и контрольным цифрам."""

from __future__ import annotations


def is_valid_inn(value: str) -> bool:
    """Вернуть ``True`` для корректного ИНН организации или ИП."""
    if not isinstance(value, str) or not value.isdigit():
        return False

    digits = [int(character) for character in value]
    if len(digits) == 10:
        weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        return _checksum(digits, weights) == digits[9]
    if len(digits) == 12:
        first_weights = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        second_weights = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        return (
            _checksum(digits, first_weights) == digits[10]
            and _checksum(digits, second_weights) == digits[11]
        )
    return False


def _checksum(digits: list[int], weights: tuple[int, ...]) -> int:
    return sum(
        digit * weight for digit, weight in zip(digits, weights)
    ) % 11 % 10


__all__ = ["is_valid_inn"]
