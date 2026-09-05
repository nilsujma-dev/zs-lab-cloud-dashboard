"""Provider registry. Add a module and register it here; nothing else needs to change."""

from __future__ import annotations

from pathlib import Path

from app.providers.aws import AwsProvider
from app.providers.base import Provider

PROVIDER_CLASSES: dict[str, type[Provider]] = {"aws": AwsProvider}


def build_registry(pricing_cache_path: Path) -> dict[str, Provider]:
    return {"aws": AwsProvider(pricing_cache_path)}


__all__ = ["PROVIDER_CLASSES", "Provider", "build_registry"]
