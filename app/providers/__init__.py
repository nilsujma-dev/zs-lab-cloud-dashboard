"""Provider registry. Add a module and register it here; nothing else needs to change."""

from __future__ import annotations

from pathlib import Path

from app.providers.aws import AwsProvider
from app.providers.azure import AzureProvider
from app.providers.base import Provider
from app.providers.gcp import GcpProvider

PROVIDER_CLASSES: dict[str, type[Provider]] = {"aws": AwsProvider, "gcp": GcpProvider, "azure": AzureProvider}


def build_registry(pricing_cache_path: Path) -> dict[str, Provider]:
    return {"aws": AwsProvider(pricing_cache_path), "gcp": GcpProvider(), "azure": AzureProvider()}


__all__ = ["PROVIDER_CLASSES", "Provider", "build_registry"]
