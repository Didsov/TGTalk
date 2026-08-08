"""Прикладные сценарии INNtoPhone."""

from src.application.telegram_enrichment import (
    EnrichmentOutcome,
    enrich_client,
    process_first_clients,
)

__all__ = ["EnrichmentOutcome", "enrich_client", "process_first_clients"]
