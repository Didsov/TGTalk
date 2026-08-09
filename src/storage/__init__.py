"""Хранилища состояния и данных приложения."""

from src.storage.organizations import Organization, OrganizationStorage
from src.storage.new_clients import (
    NewClient,
    NewClientStorage,
    ProcessingStatus,
    RegistrationDayStats,
    STATUS_LABELS,
    TelegramClaimError,
    TelegramSearchAttempt,
)
from src.storage.reporting import (
    AdminAuditEntry,
    BootstrapAdminRemovalError,
    IntegrationHealth,
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
    "IntegrationHealth",
    "NotificationState",
    "Organization",
    "OrganizationStorage",
    "ProcessingStatus",
    "RegistrationDayStats",
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
