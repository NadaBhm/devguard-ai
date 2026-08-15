"""call_llm is always monkeypatched here — no test reaches OpenRouter for
real. Safety contract: no parsing failure or missing key may ever resolve
to "approved" — every failure mode must yield "unclear".
"""

from __future__ import annotations

import json

import pytest

from core.llm_approval_parser import parse_approval_response


def _patch_call_llm(monkeypatch: pytest.MonkeyPatch, return_value) -> None:
    monkeypatch.setattr(
        "core.llm_approval_parser.call_llm", lambda *args, **kwargs: return_value
    )


def test_clear_approval_without_region(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_call_llm(
        monkeypatch,
        json.dumps({"status": "approved", "region": None, "reasoning": "Le client dit oui."}),
    )

    decision = parse_approval_response("ok go ahead")

    assert decision.status == "approved"
    assert decision.region is None


def test_approval_with_region_mentioned(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_call_llm(
        monkeypatch,
        json.dumps(
            {"status": "approved", "region": "eu-west-1", "reasoning": "Client veut l'Europe."}
        ),
    )

    decision = parse_approval_response("do it but in Europe")

    assert decision.status == "approved"
    assert decision.region == "eu-west-1"


def test_clear_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_call_llm(
        monkeypatch,
        json.dumps({"status": "rejected", "region": None, "reasoning": "Trop cher."}),
    )

    decision = parse_approval_response("no, too expensive")

    assert decision.status == "rejected"


def test_falls_back_to_unclear_when_call_llm_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_call_llm(monkeypatch, None)

    decision = parse_approval_response("hmm not sure")

    assert decision.status == "unclear"


def test_falls_back_to_unclear_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_call_llm(monkeypatch, "this is not json")

    decision = parse_approval_response("whatever")

    assert decision.status == "unclear"


def test_falls_back_to_unclear_when_status_outside_known_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the validation layer: the LLM cannot smuggle an arbitrary
    status (e.g. "maybe") that a naive caller might treat as truthy."""
    _patch_call_llm(
        monkeypatch, json.dumps({"status": "maybe", "region": None, "reasoning": "x"})
    )

    decision = parse_approval_response("kind of?")

    assert decision.status == "unclear"


def test_falls_back_to_unclear_when_region_outside_known_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_call_llm(
        monkeypatch,
        json.dumps({"status": "approved", "region": "sa-east-1", "reasoning": "x"}),
    )

    decision = parse_approval_response("deploy to Brazil")

    assert decision.status == "unclear"


def test_falls_back_to_unclear_when_reasoning_field_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_call_llm(monkeypatch, json.dumps({"status": "approved", "region": None}))

    decision = parse_approval_response("yes")

    assert decision.status == "unclear"


def test_falls_back_to_unclear_when_response_is_a_json_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_call_llm(monkeypatch, json.dumps(["approved", None, "x"]))

    decision = parse_approval_response("yes")

    assert decision.status == "unclear"


def test_never_defaults_to_approved_status_literal() -> None:
    """Static safety check on the type itself: "approved" must never be
    reachable as a default -- ApprovalDecision has no default status."""
    with pytest.raises(Exception):
        from core.llm_approval_parser import ApprovalDecision

        ApprovalDecision()  # type: ignore[call-arg]
