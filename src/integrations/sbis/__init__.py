"""Компоненты интеграции с CRM СБИС."""

from src.integrations.sbis.clients import (
    extractContacts,
    getClientByInn,
    getClientsByListId,
    getContactsByInn,
)
from src.integrations.sbis.companies import (
    get_company_card,
    get_company_uuids,
    get_open_companies_by_date,
)


__all__ = [
    "extractContacts",
    "getClientByInn",
    "getClientsByListId",
    "getContactsByInn",
    "get_company_card",
    "get_company_uuids",
    "get_open_companies_by_date",
]
