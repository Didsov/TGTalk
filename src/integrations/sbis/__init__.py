"""Компоненты интеграции с CRM СБИС."""

from src.integrations.sbis.clients import (
    extractContacts,
    getClientByInn,
    getClientsByListId,
    getContactsByInn,
)


__all__ = [
    "extractContacts",
    "getClientByInn",
    "getClientsByListId",
    "getContactsByInn",
]
