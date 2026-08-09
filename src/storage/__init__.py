"""Хранилища состояния и данных приложения."""

from src.storage.organizations import Organization, OrganizationStorage
from src.storage.new_clients import (
    NewClient,
    NewClientStorage,
    ProcessingStatus,
    STATUS_LABELS,
    TelegramClaimError,
    TelegramSearchAttempt,
)
from src.storage.reporting import (
    AdminAuditEntry,
    BootstrapAdminRemovalError,
    MAX_ATTEMPTS,
    NotificationState,
    PipelineRun,
    ReportDelivery,
    ReportDeliveryClaimError,
    ReportItem,
    ReportItemDraft,
    ReportingStorage,
    ReportRun,
)

__all__ = [
    "NewClient",
    "NewClientStorage",
    "MAX_ATTEMPTS",
    "NotificationState",
    "Organization",
    "OrganizationStorage",
    "ProcessingStatus",
    "PipelineRun",
    "ReportDelivery",
    "ReportDeliveryClaimError",
    "ReportItem",
    "ReportItemDraft",
    "ReportingStorage",
    "ReportRun",
    "STATUS_LABELS",
    "TelegramClaimError",
    "TelegramSearchAttempt",
    "AdminAuditEntry",
    "BootstrapAdminRemovalError",
]
