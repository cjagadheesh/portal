"""On-demand price lookup, used only to warn before the on-demand fallback starts billing."""

import json
from typing import Any

from portal.aws import session

# The Price List API is served from a few regions only; this is its address, not the user's
# region. The price itself is looked up for the user's region below, never hardcoded.
ENDPOINT_REGION = "us-east-1"


def client() -> Any:
    return session.client("pricing", ENDPOINT_REGION)


def region_long_name(ssm: Any, region: str) -> str | None:
    """The Price List API names regions in words ("Asia Pacific (Mumbai)"), not codes. AWS
    publishes that mapping as public SSM parameters, so there is no table to maintain."""
    try:
        resp = ssm.get_parameter(
            Name=f"/aws/service/global-infrastructure/regions/{region}/longName"
        )
    except ssm.exceptions.ParameterNotFound:
        return None
    return str(resp["Parameter"]["Value"])


def on_demand_usd_per_hour(pricing: Any, ssm: Any, region: str, instance_type: str) -> float | None:
    """Linux, shared tenancy, on-demand USD per hour for this type here; None if unknown."""
    location = region_long_name(ssm, region)
    if location is None:
        return None
    filters = {
        "location": location,
        "instanceType": instance_type,
        "operatingSystem": "Linux",
        "tenancy": "Shared",
        "preInstalledSw": "NA",
        "capacitystatus": "Used",
    }
    pages = pricing.get_paginator("get_products").paginate(
        ServiceCode="AmazonEC2",
        Filters=[{"Type": "TERM_MATCH", "Field": f, "Value": v} for f, v in filters.items()],
    )
    for page in pages:
        for raw in page["PriceList"]:
            product = json.loads(raw) if isinstance(raw, str) else raw
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for dimension in term.get("priceDimensions", {}).values():
                    usd = dimension.get("pricePerUnit", {}).get("USD")
                    if usd is not None:
                        return float(usd)
    return None
