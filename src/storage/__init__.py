"""Хранилища состояния и данных приложения."""

from src.storage.organizations import Organization, OrganizationStorage
from src.storage.new_clients import (
    NewClient,
    NewClientStorage,
    ProcessingStatus,
    STATUS_LABELS,
)

__all__ = [
    "NewClient",
    "NewClientStorage",
    "Organization",
    "OrganizationStorage",
    "ProcessingStatus",
    "STATUS_LABELS",
]
