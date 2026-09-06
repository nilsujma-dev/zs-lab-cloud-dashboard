"""AWS Pricing API lookups (us-east-1), cached for 24h in /data/pricing-cache.json."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("switchboard.pricing")

HOURS_PER_MONTH = 730
PUBLIC_IPV4_HOURLY_USD = 0.005
# Secrets Manager is a flat per-secret charge, identical in every commercial region and not
# broken down by SKU the way EC2 is, so it is a constant rather than a Pricing API lookup.
# API calls ($0.05 / 10k) are usage, not a standing rate, and are excluded like NAT processing.
SECRET_MONTHLY_USD = 0.40
CACHE_TTL_S = 24 * 60 * 60
MISS_TTL_S = 60 * 60

# region code -> Pricing API `location` attribute, all commercial regions.
REGION_LOCATIONS: dict[str, str] = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "af-south-1": "Africa (Cape Town)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "ap-east-2": "Asia Pacific (Taipei)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-south-2": "Asia Pacific (Hyderabad)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-southeast-3": "Asia Pacific (Jakarta)",
    "ap-southeast-4": "Asia Pacific (Melbourne)",
    "ap-southeast-5": "Asia Pacific (Malaysia)",
    "ap-southeast-6": "Asia Pacific (New Zealand)",
    "ap-southeast-7": "Asia Pacific (Thailand)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ca-central-1": "Canada (Central)",
    "ca-west-1": "Canada West (Calgary)",
    "eu-central-1": "EU (Frankfurt)",
    "eu-central-2": "EU (Zurich)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-west-3": "EU (Paris)",
    "eu-north-1": "EU (Stockholm)",
    "eu-south-1": "EU (Milan)",
    "eu-south-2": "EU (Spain)",
    "il-central-1": "Israel (Tel Aviv)",
    "me-south-1": "Middle East (Bahrain)",
    "me-central-1": "Middle East (UAE)",
    "mx-central-1": "Mexico (Central)",
    "sa-east-1": "South America (Sao Paulo)",
}


def location_candidates(region: str) -> list[str]:
    """Pricing `location` names to try for a region. AWS renamed "EU (…)" to "Europe (…)" in
    some SKUs, so both spellings are tried for European regions."""
    loc = REGION_LOCATIONS.get(region)
    if loc is None:
        return []
    if loc.startswith("EU ("):
        return [loc, "Europe (" + loc[len("EU ("):]]
    return [loc]


def platform_to_operating_system(platform_details: str | None) -> str:
    """EC2 `PlatformDetails` -> Pricing API `operatingSystem`."""
    p = (platform_details or "").lower()
    if "windows" in p:
        return "Windows"
    if "red hat" in p or "rhel" in p:
        return "RHEL"
    if "suse" in p:
        return "SUSE"
    if "ubuntu pro" in p:
        return "Ubuntu Pro"
    return "Linux"


def _ondemand_dimensions(product: dict[str, Any]) -> list[tuple[float, str, str]]:
    """(usd, unit, description) for every OnDemand price dimension of a Pricing product."""
    out: list[tuple[float, str, str]] = []
    for term in (product.get("terms", {}).get("OnDemand") or {}).values():
        for dim in (term.get("priceDimensions") or {}).values():
            usd = dim.get("pricePerUnit", {}).get("USD")
            if usd is None:
                continue
            try:
                out.append((float(usd), dim.get("unit", ""), dim.get("description", "")))
            except ValueError:
                continue
    return out


class Pricing:
    """Thin, cached wrapper over `pricing.get_products`.

    `client_factory` returns a boto3 Pricing client bound to us-east-1; it is only called on a
    cache miss so tests can pass a stub.
    """

    def __init__(self, cache_path: Path, client_factory: Callable[[], Any]) -> None:
        self.cache_path = cache_path
        self._client_factory = client_factory
        self._client: Any = None
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = self._load()

    # ------------------------------------------------------------------ cache
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        tmp = self.cache_path.with_suffix(".tmp")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self._cache, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.cache_path)

    def _cached(self, key: str) -> tuple[bool, float | None]:
        entry = self._cache.get(key)
        if not entry:
            return False, None
        ttl = CACHE_TTL_S if entry.get("usd") is not None else MISS_TTL_S
        if time.time() - float(entry.get("fetched_at", 0)) > ttl:
            return False, None
        return True, entry.get("usd")

    def _remember(self, key: str, usd: float | None) -> None:
        self._cache[key] = {"usd": usd, "fetched_at": time.time()}
        try:
            self._save()
        except OSError as exc:
            log.warning("pricing cache not written: %s", exc)

    def _lookup(self, key: str, fetch: Callable[[], float | None]) -> float | None:
        with self._lock:
            hit, usd = self._cached(key)
            if hit:
                return usd
        try:
            usd = fetch()
        except Exception as exc:  # noqa: BLE001 - pricing must never break inventory
            log.warning("pricing lookup failed for %s: %s", key, exc)
            usd = None
        with self._lock:
            self._remember(key, usd)
        return usd

    # ------------------------------------------------------------------ API
    def _products(self, service_code: str, filters: dict[str, str]) -> list[dict[str, Any]]:
        if self._client is None:
            self._client = self._client_factory()
        api_filters = [{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()]
        products: list[dict[str, Any]] = []
        paginator = self._client.get_paginator("get_products")
        for page in paginator.paginate(ServiceCode=service_code, Filters=api_filters):
            for item in page.get("PriceList", []):
                products.append(json.loads(item) if isinstance(item, str) else item)
        return products

    def _products_by_location(self, service_code: str, region: str, filters: dict[str, str]) -> list[dict[str, Any]]:
        for location in location_candidates(region):
            found = self._products(service_code, {**filters, "location": location})
            if found:
                return found
        return []

    def instance_hourly(self, instance_type: str, region: str, operating_system: str = "Linux") -> float | None:
        key = f"ec2:{region}:{instance_type}:{operating_system}"

        def fetch() -> float | None:
            filters = {
                "instanceType": instance_type,
                "operatingSystem": operating_system,
                "tenancy": "Shared",
                "preInstalledSw": "NA",
                "capacitystatus": "Used",
            }
            if operating_system == "Windows":
                # Without this the cheapest match is the BYOL / infrastructure-only SKU (~30% low).
                filters["licenseModel"] = "No License required"
            prices: list[float] = []
            for product in self._products_by_location("AmazonEC2", region, filters):
                prices.extend(usd for usd, unit, _ in _ondemand_dimensions(product) if unit.lower() in ("hrs", "hours", "hour"))
            return min(prices) if prices else None

        return self._lookup(key, fetch)

    def nat_hourly(self, region: str) -> float | None:
        key = f"nat:{region}"

        def fetch() -> float | None:
            for product in self._products_by_location("AmazonEC2", region, {"productFamily": "NAT Gateway"}):
                usagetype = product.get("product", {}).get("attributes", {}).get("usagetype", "")
                if not usagetype.endswith("NatGateway-Hours"):
                    continue
                dims = _ondemand_dimensions(product)
                if dims:
                    return min(usd for usd, _, _ in dims)
            return None

        return self._lookup(key, fetch)

    def volume_gb_month(self, region: str, volume_type: str = "gp3") -> float | None:
        key = f"ebs:{region}:{volume_type}"

        def fetch() -> float | None:
            for product in self._products_by_location("AmazonEC2", region, {"volumeApiName": volume_type, "productFamily": "Storage"}):
                for usd, unit, _ in _ondemand_dimensions(product):
                    if unit == "GB-Mo":
                        return usd
            return None

        return self._lookup(key, fetch)
