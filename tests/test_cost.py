"""Cost aggregation and pricing helpers with a stubbed Pricing API (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from app.providers.aws import AwsProvider
from app.providers.pricing import HOURS_PER_MONTH, Pricing, location_candidates, platform_to_operating_system


class _StubPricing:
    def instance_hourly(self, instance_type: str, region: str, operating_system: str = "Linux") -> float | None:
        return {("m5.large", "Linux"): 0.115, ("t3.micro", "Linux"): 0.012, ("m5.large", "Windows"): 0.207}.get((instance_type, operating_system))

    def nat_hourly(self, region: str) -> float | None:
        return 0.052

    def volume_gb_month(self, region: str, volume_type: str = "gp3") -> float | None:
        return 0.0952 if volume_type == "gp3" else None


def _region() -> dict:
    tags = {"Project": "zpa-pse-lab"}
    return {
        "region": "eu-central-1",
        "error": None,
        "instances": [
            {"id": "i-1", "type": "m5.large", "state": "running", "public_ip": "63.1.1.1", "platform": "Linux/UNIX", "tags": tags},
            {"id": "i-2", "type": "m5.large", "state": "running", "public_ip": None, "platform": "Linux/UNIX", "tags": tags},
            {"id": "i-3", "type": "t3.micro", "state": "stopped", "public_ip": None, "platform": "Linux/UNIX", "tags": {}},
            {"id": "i-4", "type": "m5.large", "state": "running", "public_ip": "63.1.1.4", "platform": "Windows", "tags": {}},
        ],
        "vpcs": [],
        "nat_gateways": [{"id": "nat-1", "state": "available", "public_ip": "63.1.1.9", "tags": tags}],
        "eips": [
            {"ip": "63.1.1.1", "attached": True, "instance": "i-1", "tags": {}},
            {"ip": "63.1.1.9", "attached": True, "instance": None, "tags": {}},
            {"ip": "63.1.1.7", "attached": False, "instance": None, "tags": {}},
        ],
        "volumes": [
            {"id": "vol-1", "size_gb": 80, "type": "gp3", "attached": True, "instance": "i-1", "tags": {}},
            {"id": "vol-2", "size_gb": 20, "type": "gp3", "attached": False, "instance": None, "tags": {}},
        ],
    }


def test_cost_lines_groups_and_total(tmp_path: Path) -> None:
    provider = AwsProvider(tmp_path / "pricing-cache.json")
    cost, groups = provider._cost([_region()], _StubPricing())  # type: ignore[arg-type]

    by_item = {(l["item"], l["group"]): l for l in cost["lines"]}
    linux = by_item[("m5.large Linux", "Project=zpa-pse-lab")]
    assert linux["qty"] == 2 * HOURS_PER_MONTH and linux["unit"] == "hr" and linux["unit_usd"] == 0.115
    assert linux["monthly_usd"] == round(2 * HOURS_PER_MONTH * 0.115, 2)
    assert by_item[("m5.large Windows", "untagged")]["unit_usd"] == 0.207
    assert ("t3.micro Linux", "untagged") not in by_item  # stopped: no compute charge
    assert by_item[("NAT gateway", "Project=zpa-pse-lab")]["monthly_usd"] == round(HOURS_PER_MONTH * 0.052, 2)
    assert by_item[("gp3 storage", "Project=zpa-pse-lab")]["qty"] == 80  # attributed via instance i-1
    assert by_item[("gp3 storage", "untagged")]["qty"] == 20
    # 63.1.1.1 (i-1) + 63.1.1.9 (NAT) in the project; 63.1.1.4 (i-4) + 63.1.1.7 (unattached) untagged
    assert by_item[("Public IPv4 address", "Project=zpa-pse-lab")]["qty"] == 2 * HOURS_PER_MONTH
    assert by_item[("Public IPv4 address", "untagged")]["qty"] == 2 * HOURS_PER_MONTH
    assert by_item[("Public IPv4 address", "untagged")]["unit_usd"] == 0.005

    assert cost["monthly_usd"] == round(sum(l["monthly_usd"] for l in cost["lines"]), 2)
    assert cost["currency"] == "USD" and "730h" in cost["method"]
    assert "Unattached elastic IPs are billed" in cost["notes"]
    assert any("stopped" in n for n in cost["notes"])

    g = {x["key"]: x for x in groups}
    assert g["Project=zpa-pse-lab"]["instances"] == 2
    assert g["untagged"]["instances"] == 2
    assert g["Project=zpa-pse-lab"]["monthly_usd"] == round(
        sum(l["monthly_usd"] for l in cost["lines"] if l["group"] == "Project=zpa-pse-lab"), 2
    )
    assert groups[-1]["key"] == "untagged"


def test_missing_price_is_noted_not_fatal(tmp_path: Path) -> None:
    provider = AwsProvider(tmp_path / "pricing-cache.json")
    region = _region()
    region["instances"] = [{"id": "i-9", "type": "x9.huge", "state": "running", "public_ip": None, "platform": "Linux/UNIX", "tags": {}}]
    region["volumes"], region["nat_gateways"], region["eips"] = [], [], []
    cost, _ = provider._cost([region], _StubPricing())  # type: ignore[arg-type]
    line = cost["lines"][0]
    assert line["unit_usd"] is None and line["monthly_usd"] == 0
    assert any("x9.huge" in n for n in cost["notes"])


def test_platform_mapping() -> None:
    assert platform_to_operating_system("Linux/UNIX") == "Linux"
    assert platform_to_operating_system("Windows") == "Windows"
    assert platform_to_operating_system("Windows with SQL Server Standard") == "Windows"
    assert platform_to_operating_system("Red Hat Enterprise Linux") == "RHEL"
    assert platform_to_operating_system("SUSE Linux") == "SUSE"
    assert platform_to_operating_system(None) == "Linux"


def test_location_candidates() -> None:
    assert location_candidates("eu-central-1") == ["EU (Frankfurt)", "Europe (Frankfurt)"]
    assert location_candidates("us-east-1") == ["US East (N. Virginia)"]
    assert location_candidates("xx-none-1") == []


class _FakePricingClient:
    """Minimal get_products paginator returning canned SKUs, recording filters."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def get_paginator(self, name: str):
        assert name == "get_products"
        return self

    def paginate(self, ServiceCode: str, Filters: list[dict[str, str]]):
        f = {x["Field"]: x["Value"] for x in Filters}
        self.calls.append(f)
        products = []
        if f.get("instanceType") == "m5.large" and f.get("location") == "EU (Frankfurt)":
            if f.get("operatingSystem") == "Windows":
                assert f["licenseModel"] == "No License required"
                products.append(_sku(0.207, "Hrs"))
            else:
                products.append(_sku(0.115, "Hrs"))
                products.append(_sku(0.130, "Hrs"))
        elif f.get("productFamily") == "NAT Gateway":
            products.append(_sku(0.045, "GB", usagetype="EUC1-NatGateway-Bytes"))
            products.append(_sku(0.052, "Hrs", usagetype="EUC1-NatGateway-Hours"))
        elif f.get("volumeApiName") == "gp3":
            products.append(_sku(0.0952, "GB-Mo"))
            products.append(_sku(0.006, "IOPS-Mo"))
        yield {"PriceList": [json.dumps(p) for p in products]}


def _sku(usd: float, unit: str, usagetype: str = "x") -> dict:
    return {
        "product": {"attributes": {"usagetype": usagetype}},
        "terms": {"OnDemand": {"a": {"priceDimensions": {"b": {"unit": unit, "pricePerUnit": {"USD": str(usd)}}}}}},
    }


def test_pricing_lookups_and_cache(tmp_path: Path) -> None:
    fake = _FakePricingClient()
    cache = tmp_path / "pricing-cache.json"
    p = Pricing(cache, lambda: fake)
    assert p.instance_hourly("m5.large", "eu-central-1", "Linux") == 0.115
    assert p.instance_hourly("m5.large", "eu-central-1", "Windows") == 0.207
    assert p.nat_hourly("eu-central-1") == 0.052
    assert p.volume_gb_month("eu-central-1", "gp3") == 0.0952
    n = len(fake.calls)
    assert fake.calls[0]["tenancy"] == "Shared" and fake.calls[0]["capacitystatus"] == "Used" and fake.calls[0]["preInstalledSw"] == "NA"
    assert "licenseModel" not in fake.calls[0]

    # Second instance reads from the on-disk cache without touching the client.
    p2 = Pricing(cache, lambda: (_ for _ in ()).throw(AssertionError("client must not be built")))
    assert p2.instance_hourly("m5.large", "eu-central-1", "Linux") == 0.115
    assert p2.nat_hourly("eu-central-1") == 0.052
    assert len(fake.calls) == n
    assert cache.is_file() and "ec2:eu-central-1:m5.large:Linux" in cache.read_text()


def test_pricing_unknown_type_returns_none_and_falls_back_locations(tmp_path: Path) -> None:
    fake = _FakePricingClient()
    p = Pricing(tmp_path / "c.json", lambda: fake)
    assert p.instance_hourly("zz.none", "eu-central-1") is None
    assert [c["location"] for c in fake.calls] == ["EU (Frankfurt)", "Europe (Frankfurt)"]
