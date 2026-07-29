from pathlib import Path
import warnings

from jsonschema import Draft202012Validator, FormatChecker
import yaml
from openapi_spec_validator import validate_spec
from referencing import Registry, Resource


ROOT = Path(__file__).parents[3]


def test_phase1_openapi_documents_are_valid() -> None:
    """A removed Phase 1 interface must fail before a provider can use it."""
    for relative in (
        "contracts/openapi/pdm-prediction-v2.yaml",
        "contracts/openapi/cmms-integration-v1.yaml",
    ):
        path = ROOT / relative
        assert path.is_file(), f"Phase 1 OpenAPI behaviour is unavailable: {relative}"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=DeprecationWarning,
                message="validate_spec shortcut is deprecated.*",
            )
            validate_spec(document)


def _openapi(relative: str) -> dict:
    path = ROOT / relative
    assert path.is_file(), f"Phase 1 OpenAPI behaviour is unavailable: {relative}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_pdm_v2_operation_requires_opaque_bearer_and_safe_errors() -> None:
    """Removing v2 auth or exposing diagnostic fields would break its service boundary."""
    document = _openapi("contracts/openapi/pdm-prediction-v2.yaml")
    operation = document["paths"]["/api/v2/predictions"]["post"]
    scheme = document["components"]["securitySchemes"]["PredictionBearer"]

    assert operation["security"] == [{"PredictionBearer": []}]
    assert scheme == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "opaque-service-token",
    }
    for status, code in {
        "401": "PREDICTION_UNAUTHORIZED",
        "404": "MODEL_NOT_FOUND",
        "409": "REQUEST_DIGEST_MISMATCH",
        "422": "INVALID_PREDICTION_REQUEST",
        "503": "PREDICTION_CATALOG_NOT_READY",
        "500": "PREDICTION_FAILED",
    }.items():
        response = operation["responses"][status]
        assert response["x-error-code"] == code
        assert response["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/PredictionError"
        }
    error = document["components"]["schemas"]["PredictionError"]
    assert error["additionalProperties"] is False
    assert error["required"] == ["code", "message"]
    assert set(error["properties"]) == {"code", "message"}


def test_pdm_v2_schemas_freeze_canonical_identity_decimal_and_response_boundary() -> None:
    """Relaxing wire types or adding tenant/risk output would break PDM consumers."""
    document = _openapi("contracts/openapi/pdm-prediction-v2.yaml")
    schemas = document["components"]["schemas"]
    request = schemas["PredictionRequestV2"]
    response = schemas["PredictionResponseV2"]

    assert request["additionalProperties"] is False
    assert request["required"] == [
        "tenant_id",
        "correlation_id",
        "equipment_id",
        "model_profile_id",
        "model_info_id",
        "meas_code",
        "unit",
        "sampling_frequency",
        "window_start",
        "window_end",
        "preprocessing_version",
        "request_digest",
        "history",
    ]
    assert schemas["CanonicalUuid"] == {
        "type": "string",
        "format": "uuid",
        "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    }
    assert schemas["DecimalString"]["pattern"] == "^-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?$"
    assert schemas["EpochMilliseconds"] == {"type": "integer", "minimum": 0}
    assert response["additionalProperties"] is False
    assert response["properties"]["generated_at"] == {
        "type": "string",
        "format": "date-time",
    }
    assert {"tenant_id", "risk", "recommendation"}.isdisjoint(response["properties"])


def test_cmms_asset_operations_preserve_company_scoping_and_conditional_idempotency() -> None:
    """Making the header global or accepting caller company fields breaks existing clients."""
    document = _openapi("contracts/openapi/cmms-integration-v1.yaml")
    post = document["paths"]["/api/assets"]["post"]
    get = document["paths"]["/api/assets/by-equipment-id/{equipment_id}"]["get"]
    request = document["components"]["schemas"]["AssetCreateRequest"]

    assert post["security"] == [{"CmmsJwtBearer": []}]
    assert get["security"] == [{"CmmsJwtBearer": []}]
    key = next(parameter for parameter in post["parameters"] if parameter["name"] == "Idempotency-Key")
    assert key["required"] is False
    assert "only when equipment_id is present" in key["description"]
    assert "pilot-asset:{canonical tb_device_id}" in key["description"]
    assert "company-scoped" in get["description"]
    assert request["required"] == ["name"]
    assert request["additionalProperties"] is False
    assert {"tenant_id", "company_id"}.isdisjoint(request["properties"])
    assert post["responses"]["400"]["x-error-code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert post["responses"]["409"]["x-error-code"] == "IDEMPOTENCY_CONFLICT"
    assert get["responses"]["404"]["x-error-code"] == "ASSET_NOT_FOUND"


def test_equipment_mapping_schema_validates_instances_and_declares_tenant_uniqueness() -> None:
    """A permissive mapping could join an asset or device to the wrong tenant."""
    path = ROOT / "contracts/json-schema/equipment-mapping-v1.json"
    assert path.is_file(), "Phase 1 equipment mapping behaviour is unavailable"
    schema = __import__("json").loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    valid = {
        "tenant_id": "00000000-0000-4000-8000-000000000001",
        "equipment_id": "00000000-0000-4000-8000-000000000101",
        "cmms_asset_id": 101,
        "tb_device_id": "00000000-0000-4000-8000-000000000201",
        "enabled": True,
    }
    assert not list(validator.iter_errors(valid))
    for invalid in (
        {**valid, "tenant_id": "00000000-0000-4000-8000-00000000000A"},
        {**valid, "cmms_asset_id": 0},
        {**valid, "tb_device_id": "00000000000040008000000000000201"},
        {**valid, "state": "ACTIVE"},
    ):
        assert list(validator.iter_errors(invalid))
    invariants = schema["$comment"]
    assert "(tenant_id, equipment_id)" in invariants
    assert "(tenant_id, cmms_asset_id)" in invariants
    assert "(tenant_id, tb_device_id)" in invariants
