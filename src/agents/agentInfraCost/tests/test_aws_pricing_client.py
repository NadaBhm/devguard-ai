"""Tests for core.aws_pricing_client — NEVER a real network call.

Every boto3 client is a MagicMock built by hand; nothing here reaches AWS.
"""

import json
from unittest.mock import MagicMock

import pytest

from core import aws_pricing_client
from core.aws_pricing_client import (
    AwsPricingFetchError,
    _extract_on_demand_usd,
    fetch_live_pricing_data,
)

_FALLBACK = {
    "_meta": {"region": "us-east-1"},
    "ecs_fargate": {
        "x86": {"vcpu_per_hour": 0.04048, "memory_gb_per_hour": 0.004445},
        "arm_graviton": {"vcpu_per_hour": 0.032384, "memory_gb_per_hour": 0.003556},
        "hours_per_month": 730,
    },
    "ec2_on_demand_hourly": {"t3.micro": 0.0104, "t3.small": 0.0208},
}


def _price_list_entry(usd_price: str) -> str:
    """Build one raw AWS PriceList item exactly as the real API shapes it."""
    return json.dumps(
        {
            "terms": {
                "OnDemand": {
                    "OFFER1.JRTCKXETXF": {
                        "priceDimensions": {
                            "OFFER1.JRTCKXETXF.6YS6EN2CT7": {
                                "unit": "Hrs",
                                "pricePerUnit": {"USD": usd_price},
                            }
                        }
                    }
                }
            }
        }
    )


@pytest.fixture(autouse=True)
def _reset_cache():
    """The module keeps a process-wide cache — never let it leak between tests."""
    aws_pricing_client._cache["data"] = None
    aws_pricing_client._cache["fetched_at"] = 0.0
    yield
    aws_pricing_client._cache["data"] = None
    aws_pricing_client._cache["fetched_at"] = 0.0


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


def test_fetch_live_pricing_data_updates_ec2_price_from_mocked_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_client = MagicMock()
    mock_client.get_products.return_value = {"PriceList": [_price_list_entry("0.0199000000")]}
    monkeypatch.setattr(aws_pricing_client, "_get_pricing_client", lambda: mock_client)

    result = fetch_live_pricing_data(fallback=_FALLBACK)

    assert result["ec2_on_demand_hourly"]["t3.micro"] == 0.0199
    # untouched entries keep their static value
    assert result["_meta"] == _FALLBACK["_meta"]


def test_extract_on_demand_usd_parses_a_real_shaped_entry() -> None:
    assert _extract_on_demand_usd(_price_list_entry("0.0104000000")) == 0.0104


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_cache_returns_same_object_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = MagicMock()
    mock_client.get_products.return_value = {"PriceList": [_price_list_entry("0.01")]}
    monkeypatch.setattr(aws_pricing_client, "_get_pricing_client", lambda: mock_client)

    first = fetch_live_pricing_data(fallback=_FALLBACK)
    calls_after_first_round = mock_client.get_products.call_count

    second = fetch_live_pricing_data(fallback=_FALLBACK)

    assert first is second
    # the second call must be served from cache, not re-fetch every price
    assert mock_client.get_products.call_count == calls_after_first_round


def test_one_failing_instance_type_does_not_affect_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _get_products(ServiceCode, Filters, MaxResults):
        instance_type = next(f["Value"] for f in Filters if f["Field"] == "instanceType")
        if instance_type == "t3.micro":
            raise RuntimeError("simulated AWS hiccup for this one instance type")
        return {"PriceList": [_price_list_entry("0.0250000000")]}

    mock_client = MagicMock()
    mock_client.get_products.side_effect = _get_products
    monkeypatch.setattr(aws_pricing_client, "_get_pricing_client", lambda: mock_client)

    result = fetch_live_pricing_data(fallback=_FALLBACK)

    assert result["ec2_on_demand_hourly"]["t3.micro"] == _FALLBACK["ec2_on_demand_hourly"]["t3.micro"]
    assert result["ec2_on_demand_hourly"]["t3.small"] == 0.025


def test_client_construction_failure_returns_fallback_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom():
        raise RuntimeError("no credentials configured")

    monkeypatch.setattr(aws_pricing_client, "_get_pricing_client", _boom)

    result = fetch_live_pricing_data(fallback=_FALLBACK)

    # client construction fails before any copying happens, so this is
    # literally the same object handed back untouched
    assert result is _FALLBACK


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_extract_on_demand_usd_raises_on_malformed_json() -> None:
    with pytest.raises(AwsPricingFetchError):
        _extract_on_demand_usd("not valid json at all")


def test_extract_on_demand_usd_raises_on_missing_terms_key() -> None:
    with pytest.raises(AwsPricingFetchError):
        _extract_on_demand_usd(json.dumps({"product": {"attributes": {}}}))


def test_empty_price_list_raises_fetch_error_caught_by_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_client = MagicMock()
    mock_client.get_products.return_value = {"PriceList": []}
    monkeypatch.setattr(aws_pricing_client, "_get_pricing_client", lambda: mock_client)

    # fetch_live_pricing_data itself must never raise -- it logs and falls
    # back per price point, exactly as when any other error occurs.
    result = fetch_live_pricing_data(fallback=_FALLBACK)
    assert result["ec2_on_demand_hourly"] == _FALLBACK["ec2_on_demand_hourly"]
