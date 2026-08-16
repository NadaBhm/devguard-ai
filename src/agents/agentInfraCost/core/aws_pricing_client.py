"""Phase A (post-mission extension): live AWS Pricing API client.

Fetches current on-demand prices via the official AWS Pricing API (boto3)
and normalizes them into EXACTLY the same shape as ``data/aws_pricing.json``
— so ``cost_estimator.py``'s formulas never change, only where the numbers
come from. Cached in memory for ``_CACHE_TTL_SECONDS`` to avoid hitting AWS
on every single cost estimate.

Reliability note, stated plainly rather than hidden: EC2 on-demand pricing
uses a well-documented, stable set of filters and is fetched fully live.
ECS Fargate and Lambda pricing use filters that are harder to guarantee
correct without testing against a real AWS account (which this codebase
cannot do in its own development/test environment) — each individual price
point is fetched on a best-effort basis and silently keeps its static
fallback value (with a logged warning) if the live call fails, rather than
ever breaking cost estimation as a whole. If you have real AWS access,
watch the warnings and adjust the Fargate/Lambda filters below if needed.

The AWS Pricing API itself only answers from two regions regardless of
which region's prices you're asking about — ``us-east-1`` or
``ap-south-1`` — an AWS platform detail, not a choice made here.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from typing import Any, Final

logger = logging.getLogger(__name__)

_PRICING_API_REGION: Final[str] = "us-east-1"
_CACHE_TTL_SECONDS: Final[float] = 24 * 3600

# AWS Pricing API filters want the region written out in full ("location"),
# not the region code. Extend this table if more regions are needed.
_REGION_TO_LOCATION: Final[dict[str, str]] = {
    "us-east-1": "US East (N. Virginia)",
}

_EC2_INSTANCE_TYPES: Final[tuple[str, ...]] = (
    "t3.nano", "t3.micro", "t3.small", "t3.medium", "t3.large", "t3.xlarge",
    "t4g.nano", "t4g.micro", "t4g.small", "t4g.medium",
    "m5.large", "m6i.large", "m7g.large",
)

# usagetype filter values this module attempts to refresh live, on a
# best-effort basis. Lambda pricing is deliberately NOT attempted live here
# — its GetProducts filters are even less reliable to guess than Fargate's
# without testing against a real account — it always keeps its static value
# from data/aws_pricing.json for now.
_FARGATE_ARCH_USAGETYPE: Final[dict[str, str]] = {
    "x86": "Fargate-vCPU-Hours:perCPU",
    "arm_graviton": "Fargate-ARM-vCPU-Hours:perCPU",
}

_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}


class AwsPricingFetchError(Exception):
    pass


def _get_pricing_client() -> Any:
    """A fresh boto3 Pricing client. Imported lazily so this whole module
    stays optional — nothing else in the pipeline requires boto3 to import
    successfully, since cost_estimator falls back to the static table if
    this client can't even be constructed."""
    import boto3

    return boto3.client("pricing", region_name=_PRICING_API_REGION)


def _extract_on_demand_usd(price_list_item: str) -> float:
    """Parse one raw ``PriceList`` entry (a JSON string, not a dict — an
    AWS API quirk) down to its on-demand USD-per-unit price."""
    try:
        product = json.loads(price_list_item)
        on_demand_offers = product["terms"]["OnDemand"]
        offer = next(iter(on_demand_offers.values()))
        dimension = next(iter(offer["priceDimensions"].values()))
        return float(dimension["pricePerUnit"]["USD"])
    except (json.JSONDecodeError, KeyError, StopIteration, ValueError) as exc:
        raise AwsPricingFetchError(f"Could not parse AWS price list entry: {exc}") from exc


def _fetch_ec2_hourly_price(client: Any, instance_type: str, location: str) -> float:
    response = client.get_products(
        ServiceCode="AmazonEC2",
        Filters=[
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
            {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
            {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
            {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
        ],
        MaxResults=1,
    )
    price_list = response.get("PriceList", [])
    if not price_list:
        raise AwsPricingFetchError(f"No EC2 on-demand price found for {instance_type!r}")
    return _extract_on_demand_usd(price_list[0])


def _fetch_fargate_vcpu_hourly_price(client: Any, usagetype_suffix: str, location: str) -> float:
    response = client.get_products(
        ServiceCode="AmazonECS",
        Filters=[
            {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            {"Type": "TERM_MATCH", "Field": "usagetype", "Value": usagetype_suffix},
        ],
        MaxResults=1,
    )
    price_list = response.get("PriceList", [])
    if not price_list:
        raise AwsPricingFetchError(f"No Fargate price found for usagetype {usagetype_suffix!r}")
    return _extract_on_demand_usd(price_list[0])


def fetch_live_pricing_data(fallback: dict[str, Any]) -> dict[str, Any]:
    """Fetch current AWS prices, shaped exactly like ``data/aws_pricing.json``.

    Args:
        fallback: The static table's already-loaded content. Used as the
            starting point (so ``_meta``, ``optimization_discounts`` etc.
            are always present) and as the per-price fallback whenever a
            specific live fetch fails.

    Returns:
        A dict in the same shape as ``fallback``, with as many prices
        refreshed from AWS as could be fetched successfully. Never raises:
        any failure (missing boto3, no credentials, no network, a bad
        filter) is logged and the corresponding value(s) simply keep their
        static fallback.
    """
    now = time.monotonic()
    if _cache["data"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS:
        return _cache["data"]

    result = copy.deepcopy(fallback)
    location = _REGION_TO_LOCATION[_PRICING_API_REGION]

    try:
        client = _get_pricing_client()
    except Exception as exc:  # boto3 missing, misconfigured, etc.
        logger.warning("Could not create an AWS Pricing client; using the static table: %s", exc)
        return fallback

    for instance_type in _EC2_INSTANCE_TYPES:
        try:
            result["ec2_on_demand_hourly"][instance_type] = _fetch_ec2_hourly_price(
                client, instance_type, location
            )
        except Exception as exc:
            logger.warning(
                "Live EC2 price fetch failed for %s; keeping static value: %s", instance_type, exc
            )

    for arch, usagetype_suffix in _FARGATE_ARCH_USAGETYPE.items():
        try:
            result["ecs_fargate"][arch]["vcpu_per_hour"] = _fetch_fargate_vcpu_hourly_price(
                client, usagetype_suffix, location
            )
        except Exception as exc:
            logger.warning(
                "Live Fargate price fetch failed for arch=%s; keeping static value "
                "(filters may need adjusting for your account): %s", arch, exc
            )

    _cache["data"] = result
    _cache["fetched_at"] = now
    return result
