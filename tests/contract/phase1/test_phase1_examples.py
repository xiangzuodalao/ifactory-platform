import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import yaml
from referencing import Registry, Resource


ROOT = Path(__file__).parents[3]
REQUEST_DIGEST = "067096fc115546164d8608cddadc56b185fefea9dffab4c7462c8d28011cdfbd"
INPUT_DIGEST = "c67fe58c14f2e8a1ef252e6ff47b4877c0da8fa52664772dd3c08ae3d6552609"


def _json_fixture(name: str) -> dict:
    path = ROOT / "tests/contract/phase1/fixtures" / name
    assert path.is_file(), f"Phase 1 fixture behaviour is unavailable: {name}"
    return json.loads(path.read_text(encoding="utf-8"))


def _openapi(name: str) -> dict:
    path = ROOT / "contracts/openapi" / name
    assert path.is_file(), f"Phase 1 OpenAPI fixture behaviour is unavailable: {name}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _validator(document: dict, name: str) -> Draft202012Validator:
    schema_document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:ifactory:phase1-test:{name}",
        "components": document["components"],
        "$ref": f"#/components/schemas/{name}",
    }
    registry = Registry().with_resource(
        schema_document["$id"], Resource.from_contents(schema_document)
    )
    return Draft202012Validator(
        schema_document,
        registry=registry,
        format_checker=FormatChecker(),
    )


def _stable_json_bytes(value: object) -> bytes:
    """Fixed test-only encoder for these string/integer/list/object projections, not RFC 8785."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def test_pdm_examples_validate_and_reject_noncanonical_wire_variants() -> None:
    """Relaxing UUID, decimal, timestamp, or strict-object rules must reject fixtures."""
    request = _json_fixture("pdm-prediction-request.json")
    response = _json_fixture("pdm-prediction-response.json")
    document = _openapi("pdm-prediction-v2.yaml")
    request_validator = _validator(document, "PredictionRequestV2")
    response_validator = _validator(document, "PredictionResponseV2")

    assert not list(request_validator.iter_errors(request))
    assert not list(response_validator.iter_errors(response))
    assert len(request["history"]) == 66
    assert request["history"][0]["timestamp"] == 1785283740000
    assert request["history"][-1]["timestamp"] == 1785287640000
    assert response["generated_at"] == "2026-07-29T01:15:00Z"
    assert len(response["forecast"]) == 15
    assert response["forecast"][0]["timestamp"] == 1785287700000
    assert response["forecast"][-1]["timestamp"] == 1785288540000
    assert {
        field: response[field]
        for field in ("correlation_id", "equipment_id", "model_profile_id", "model_info_id", "meas_code")
    } == {
        field: request[field]
        for field in ("correlation_id", "equipment_id", "model_profile_id", "model_info_id", "meas_code")
    }
    assert response["model_artifact_sha256"] == "5feeb31058fe0521f94758faa22214afe4619e466e22cbb8c6fb7ffcf2369562"
    assert response["request_digest"] == REQUEST_DIGEST
    assert response["input_digest"] == INPUT_DIGEST
    assert {(point["value"], point["unit"]) for point in response["forecast"]} == {("4.00", "mm/s")}
    for invalid in (
        {**request, "tenant_id": "00000000-0000-4000-8000-00000000000A"},
        {**request, "correlation_id": "00000000000040008000000000000102"},
        {**request, "equipment_id": "{00000000-0000-4000-8000-000000000101}"},
        {**request, "correlation_id": 102},
        {**request, "model_info_id": ""},
        {**request, "history": [{**request["history"][0], "value": 4.0}]},
        {**request, "history": [{**request["history"][0], "value": "NaN"}]},
        {**request, "history": [{**request["history"][0], "value": "1e2"}]},
        {**request, "history": [{**request["history"][0], "value": "+4.00"}]},
        {**request, "history": [{**request["history"][0], "timestamp": "1785283740000"}]},
        {**request, "history": [{**request["history"][0], "timestamp": 1785283740000.5}]},
        {**request, "history": [{**request["history"][0], "timestamp": True}]},
        {**request, "unexpected": "not permitted"},
    ):
        assert list(request_validator.iter_errors(invalid))
    assert list(response_validator.iter_errors({**response, "tenant_id": request["tenant_id"]}))
    assert list(response_validator.iter_errors({**response, "risk": "HIGH"}))
    assert list(response_validator.iter_errors({**response, "recommendation": "repair"}))


def test_provider_request_and_input_digest_projections_match_frozen_literals() -> None:
    """Changing provider projection membership, sorting, or fixed scale breaks replay identity."""
    request = _json_fixture("pdm-prediction-request.json")
    request_projection = {
        "tenant_id": request["tenant_id"],
        "equipment_id": request["equipment_id"],
        "model_profile_id": request["model_profile_id"],
        "model_info_id": request["model_info_id"],
        "meas_code": request["meas_code"],
        "unit": request["unit"],
        "sampling_frequency": request["sampling_frequency"],
        "window_start": request["window_start"],
        "window_end": request["window_end"],
        "history": sorted(
            (
                {
                    "data_id": point["data_id"],
                    "timestamp": point["timestamp"],
                    "value": point["value"],
                    "unit": point["unit"],
                }
                for point in request["history"]
            ),
            key=lambda point: (point["timestamp"], point["data_id"]),
        ),
    }
    normalized_input = {
        "model_profile_id": request["model_profile_id"],
        "model_info_id": request["model_info_id"],
        "meas_code": request["meas_code"],
        "preprocessing_version": request["preprocessing_version"],
        "buckets": [
            {"timestamp": point["timestamp"], "value": point["value"]}
            for point in request_projection["history"]
        ],
    }
    assert hashlib.sha256(_stable_json_bytes(request_projection)).hexdigest() == REQUEST_DIGEST
    assert hashlib.sha256(_stable_json_bytes(normalized_input)).hexdigest() == INPUT_DIGEST


def test_consumer_request_and_input_digest_projections_match_frozen_literals() -> None:
    """Changing consumer projection membership, ordering, or buckets breaks PDM agreement."""
    request = _json_fixture("pdm-prediction-request.json")
    history = [
        {
            "data_id": row["data_id"],
            "timestamp": row["timestamp"],
            "value": row["value"],
            "unit": row["unit"],
        }
        for row in request["history"]
    ]
    history.sort(key=lambda row: (row["timestamp"], row["data_id"]))
    projection = {
        "tenant_id": request["tenant_id"],
        "equipment_id": request["equipment_id"],
        "model_profile_id": request["model_profile_id"],
        "model_info_id": request["model_info_id"],
        "meas_code": request["meas_code"],
        "unit": request["unit"],
        "sampling_frequency": request["sampling_frequency"],
        "window_start": request["window_start"],
        "window_end": request["window_end"],
        "history": history,
    }
    buckets = []
    for row in history:
        buckets.append({"timestamp": row["timestamp"], "value": row["value"]})
    normalized = {
        "model_profile_id": request["model_profile_id"],
        "model_info_id": request["model_info_id"],
        "meas_code": request["meas_code"],
        "preprocessing_version": request["preprocessing_version"],
        "buckets": buckets,
    }
    assert hashlib.sha256(_stable_json_bytes(projection)).hexdigest() == REQUEST_DIGEST
    assert hashlib.sha256(_stable_json_bytes(normalized)).hexdigest() == INPUT_DIGEST


def test_cmms_examples_validate_without_tenant_body_and_keep_additive_response_shape() -> None:
    """Adding caller company fields or removing asset identity would break CMMS integration."""
    request = _json_fixture("cmms-asset-request.json")
    response = _json_fixture("cmms-asset-response.json")
    document = _openapi("cmms-integration-v1.yaml")
    request_validator = _validator(document, "AssetCreateRequest")
    response_validator = _validator(document, "AssetResponse")

    assert not list(request_validator.iter_errors(request))
    assert not list(response_validator.iter_errors(response))
    assert request == {
        "name": "Pilot CNC 001",
        "equipment_id": "00000000-0000-4000-8000-000000000101",
    }
    assert response["id"] == 101
    assert response["name"] == request["name"]
    assert response["equipment_id"] == request["equipment_id"]
    assert not list(request_validator.iter_errors({"name": "Legacy asset still valid"}))
    assert list(request_validator.iter_errors({**request, "tenant_id": "00000000-0000-4000-8000-000000000001"}))
    assert list(request_validator.iter_errors({**request, "equipment_id": "not-a-uuid"}))
