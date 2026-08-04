from __future__ import annotations

import json
from pathlib import Path
import warnings

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate_spec
import yaml


ROOT = Path(__file__).parents[3]
FIXTURES = Path(__file__).parent / "fixtures"


def _openapi(name: str) -> dict:
    return yaml.safe_load((ROOT / "contracts/openapi" / name).read_text(encoding="utf-8"))


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _component_validator(document: dict, name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {
            "$ref": f"#/components/schemas/{name}",
            "components": document["components"],
        }
    )


def test_closed_loop_openapi_documents_are_valid() -> None:
    for name in ("cmms-integration-v1.yaml", "platform-integration-v1.yaml"):
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            validate_spec(_openapi(name))


def test_action_api_is_delegated_versioned_and_idempotent() -> None:
    document = _openapi("platform-integration-v1.yaml")
    preview = document["paths"][
        "/api/v1/maintenance-alerts/{alert_id}/work-order-plan"
    ]["get"]
    action = document["paths"]["/api/v1/maintenance-alerts/{alert_id}/actions"]["post"]
    scheme = document["components"]["securitySchemes"]["ThingsBoardDelegatedToken"]

    assert scheme == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Authorization",
        "description": "The exact browser Bearer token issued by ThingsBoard.",
    }
    assert preview["security"] == [{"ThingsBoardDelegatedToken": []}]
    assert action["security"] == [{"ThingsBoardDelegatedToken": []}]
    key = next(item for item in action["parameters"] if item.get("name") == "Idempotency-Key")
    assert key["required"] is True
    assert "alert-action:" in key["schema"]["pattern"]
    assert set(action["responses"]) == {"202", "400", "401", "403", "404", "409"}
    create = document["components"]["schemas"]["CreateWorkOrderAction"]
    assert create["additionalProperties"] is False
    assert create["required"] == ["action", "expected_version", "confirmed_plan_hash"]


def test_cmms_work_order_contract_preserves_legacy_and_freezes_integration_identity() -> None:
    document = _openapi("cmms-integration-v1.yaml")
    post = document["paths"]["/api/work-orders"]["post"]
    lookup = document["paths"]["/api/work-orders/by-external-ref"]["get"]
    status_change = document["paths"][
        "/api/work-orders/{work_order_id}/change-status"
    ]["patch"]
    request = document["components"]["schemas"]["PredictiveWorkOrderCreateRequest"]

    assert {"200", "201"} <= set(post["responses"])
    assert post["security"] == [{"CmmsJwtBearer": []}]
    key = next(item for item in post["parameters"] if item["name"] == "Idempotency-Key")
    assert key["required"] is False
    assert key["schema"]["pattern"].startswith("^wo:")
    assert request["additionalProperties"] is False
    assert set(request["required"]) == {
        "title", "description", "priority", "asset", "external_source",
        "external_ref", "correlation_id", "equipment_id", "tb_alarm_id",
        "model_profile_id", "model_info_id", "policy_version",
    }
    assert lookup["parameters"][0]["schema"]["const"] == "PDM_FORECAST"
    assert lookup["responses"]["404"]["x-error-code"] == "WORK_ORDER_NOT_FOUND"
    assert status_change["security"] == [{"CmmsJwtBearer": []}]
    assert status_change["x-write-semantics"] == "at-most-once-no-retry"
    assert set(status_change["responses"]) == {"200", "400", "404", "406"}
    status_request = document["components"]["schemas"]["WorkOrderChangeStatusRequest"]
    assert status_request == {
        "type": "object",
        "additionalProperties": False,
        "required": ["status"],
        "properties": {
            "status": {"type": "string", "enum": ["IN_PROGRESS", "COMPLETE"]}
        },
    }


def test_public_examples_validate_and_reject_unbounded_or_secret_fields() -> None:
    alert_schema = json.loads(
        (ROOT / "contracts/json-schema/maintenance-alert-v1.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(alert_schema)
    validator = Draft202012Validator(alert_schema, format_checker=FormatChecker())
    alert = _fixture("maintenance-alert.json")
    assert not list(validator.iter_errors(alert))
    for invalid in (
        {**alert, "telemetry": [{"ts": 1, "value": "4.1"}]},
        {**alert, "forecast": ["4.1"] * 15},
        {**alert, "authorization": "Bearer secret"},
        {**alert, "maintenance_alert_version": 0},
    ):
        assert list(validator.iter_errors(invalid))

    platform = _openapi("platform-integration-v1.yaml")
    plan = _fixture("work-order-plan.json")
    action = _fixture("work-order-action.json")
    assert not list(_component_validator(platform, "WorkOrderPlan").iter_errors(plan))
    assert not list(
        _component_validator(platform, "CreateWorkOrderAction").iter_errors(action)
    )

    cmms = _openapi("cmms-integration-v1.yaml")
    request_validator = _component_validator(cmms, "PredictiveWorkOrderCreateRequest")
    request = _fixture("cmms-work-order-request.json")
    assert not list(request_validator.iter_errors(request))
    assert list(request_validator.iter_errors({**request, "company_id": 42}))
