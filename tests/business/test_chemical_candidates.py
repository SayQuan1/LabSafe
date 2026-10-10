"""Frozen dictionary, consensus and hash-bound Worker replay with synthetic entities."""

import copy
import hashlib
import json
from uuid import uuid4

import pytest

from packages.domain.inference_execution import InferenceInvalidResult, validate_result
from packages.inference_protocol.chemical import (
    RUNTIME_PROFILE,
    bounded_json,
    dictionary,
    extract_entities,
)
from packages.inference_protocol.fields import extract_fields
from tests.ai.test_supervisor import IDENTITY
from tests.evidence_helpers import add_bottles, add_regions, lease, result_for


def entries():
    return {
        "purpose": "development",
        "dictionary_version_id": str(uuid4()),
        "entries": [
            {
                "entity_id": "11111111-1111-4111-8111-111111111111",
                "canonical_name": "Alpha",
                "aliases": ["甲", "shared"],
                "cas": "64-17-5",
                "source": "synthetic test-only mapping; no chemical safety data",
            },
            {
                "entity_id": "22222222-2222-4222-8222-222222222222",
                "canonical_name": "Beta",
                "aliases": ["乙", "shared"],
                "cas": None,
                "source": "synthetic test-only mapping; no chemical safety data",
            },
        ],
    }


def fields(*texts):
    return extract_fields(
        [
            {
                "image_id": "image",
                "detection_id": "bottle",
                "line_id": f"line{i}",
                "crop_id": f"crop{i}",
                "raw_text": "NAME:" + text,
                "confidence": 0.9,
            }
            for i, text in enumerate(texts)
        ]
    )


@pytest.mark.parametrize(
    "text,method",
    [
        ("64-17-5", "cas_exact"),
        ("ＡＬＰＨＡ", "alias_exact"),
        ("甲", "alias_exact"),
        ("Alph", "fuzzy"),
    ],
)
def test_cas_alias_fuzzy_order_and_normalization(text, method):
    result = extract_entities(fields(text), entries())[0]
    assert result["candidates"][0]["canonical_name"] == "Alpha"
    assert result["candidates"][0]["match_method"] == method
    assert result["resolution"] == ("candidate" if method == "fuzzy" else "resolved")


@pytest.mark.parametrize(
    "texts,resolution",
    [
        (("Alpha", "甲"), "resolved"),
        (("Alpha", "Beta"), "candidate"),
        (("Alpha", ""), "candidate"),
        (("shared",), "candidate"),
        (("12-34-1",), "unknown"),
    ],
)
def test_all_fields_vote_conflicts_ties_and_missing_never_disappear(texts, resolution):
    result = extract_entities(fields(*texts), entries())[0]
    assert result["resolution"] == resolution
    if texts in [("Alpha", "Beta"), ("Alpha", "")]:
        assert all(c["confidence"] == 0 for c in result["candidates"])


def test_threshold_edges_min_scores_and_no_cross_image_consensus():
    source = fields("Alpha", "Alph")
    source[1]["confidence"] = 0.6
    assert extract_entities(source, entries(), entity_min=0.8)[0]["resolution"] == "resolved"
    assert (
        extract_entities(source, entries(), entity_min=0.80000001)[0]["resolution"] == "candidate"
    )
    source[1]["confidence"] = 0.5999999
    assert extract_entities(source, entries(), entity_min=0.8)[0]["resolution"] == "candidate"
    source[1]["image_id"] = "different"
    assert len(extract_entities(source, entries())) == 2


def test_empty_dictionary_unknown_and_unprefixed_exact_only():
    value = entries()
    value["entries"] = []
    assert extract_entities(fields("Alpha"), value)[0]["resolution"] == "unknown"
    line = fields("Alpha")[0]
    line = {
        **line["source_lines"][0],
        "image_id": "image",
        "detection_id": "bottle",
        "raw_text": "ALPHA",
    }
    assert extract_fields([line], entries())[0]["normalized_text"] == "ALPHA"
    line["raw_text"] = "Alph"
    assert extract_fields([line], entries()) == []


@pytest.mark.parametrize(
    "case",
    ["duplicate_id", "empty", "aliases", "bad_cas", "source", "101", "too_long", "storage_class"],
)
def test_dictionary_bad_content_rejected(case):
    value = entries()
    first = value["entries"][0]
    if case == "duplicate_id":
        value["entries"][1]["entity_id"] = first["entity_id"]
    elif case == "empty":
        first["canonical_name"] = "  "
    elif case == "aliases":
        first["aliases"] = ["ＡＬＰＨＡ"]
    elif case == "bad_cas":
        first["cas"] = "64-17-6"
    elif case == "source":
        first["source"] = " "
    elif case == "101":
        value["entries"] *= 51
    elif case == "too_long":
        first["aliases"] = ["x" * 201]
    else:
        first["storage_class"] = "flammable"
    with pytest.raises(ValueError):
        dictionary(value)


def test_cas_ambiguity_preserved_and_no_fuzzy_guess_for_valid_unknown_cas():
    value = entries()
    value["entries"][1]["cas"] = "64-17-5"
    result = extract_entities(fields("64-17-5"), value)[0]
    assert result["resolution"] == "candidate" and len(result["candidates"]) == 2
    assert extract_entities(fields("7732-18-5"), value)[0]["resolution"] == "unknown"


def test_top5_does_not_hide_sixth_winner_conflict():
    value = entries()
    value["entries"] = [
        {
            "entity_id": f"00000000-0000-4000-8000-{i:012d}",
            "canonical_name": f"Name{i}",
            "aliases": ["many"] if i < 5 else [],
            "cas": None,
            "source": "synthetic",
        }
        for i in range(6)
    ]
    result = extract_entities(fields("many", "Name5"), value)[0]
    assert result["resolution"] == "candidate" and len(result["candidates"]) == 5
    assert all(c["confidence"] == 0 for c in result["candidates"])


def test_distance_budget_and_snapshot_size_duplicate_keys():
    value = entries()
    value["entries"] = [
        {
            "entity_id": str(uuid4()),
            "canonical_name": "b" * 200,
            "aliases": [],
            "cas": None,
            "source": "synthetic",
        }
        for _ in range(100)
    ]
    with pytest.raises(ValueError, match="budget"):
        extract_entities(fields("a" * 200), value)
    for raw in ('{"purpose":1,"purpose":2}', " " * 65537, '"' + "甲" * 22000 + '"'):
        with pytest.raises(ValueError):
            bounded_json(raw)


@pytest.mark.parametrize(
    "ocr,entity", [(float("nan"), 0.9), (0.6, float("inf")), (-0.1, 0.9), (0.6, 1.1)]
)
def test_nonfinite_or_out_of_range_thresholds_fail(ocr, entity):
    with pytest.raises(ValueError):
        extract_entities(fields("Alpha"), entries(), ocr, entity)


def chemical_case():
    value = lease()
    result = add_bottles(value, add_regions(value, result_for(value, "needs_review")))
    data = entries()
    result["dictionary_version_id"] = value.input.dictionary_version_id
    data["dictionary_version_id"] = value.input.dictionary_version_id
    raw = json.dumps(data, ensure_ascii=False)
    result["dictionary_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    bundle = {
        "purpose": "development",
        "bundle_id": value.input.model_bundle_id,
        "dictionary_version_id": value.input.dictionary_version_id,
        "pipeline_version": "vision-v1",
        "device_profile": "cpu",
        "adapter_id": "dfine-coco80-rgb-stretch-v1",
        "runtime_profile": RUNTIME_PROFILE,
        "artifacts": {
            name: {
                "path": "C:/synthetic/" + name,
                "sha256": result["dictionary_sha256"] if name == "dictionary" else "a" * 64,
            }
            for name in (
                "detector_model",
                "detector_config",
                "detector_preprocessor",
                "ocr_det_model",
                "ocr_det_config",
                "ocr_rec_model",
                "ocr_rec_config",
                "dictionary",
                "runtime_lock",
            )
        },
        "thresholds": {
            "detection_min": 0.4,
            "ocr_min": 0.6,
            "entity_min": 0.9,
            "blur_min": 80,
            "dark_min": 0.12,
            "glare_max": 0.3,
        },
        "threads": 2,
    }
    bundle_raw = json.dumps(bundle)
    result["model_checksum"] = hashlib.sha256(bundle_raw.encode()).hexdigest()
    result["extraction_context"] = {"bundle_json": bundle_raw, "dictionary_json": raw}
    result["execution_identity"] = dict(
        IDENTITY,
        runtime_profile=RUNTIME_PROFILE,
        **{
            k: result[k]
            for k in (
                "model_bundle_id",
                "model_checksum",
                "dictionary_version_id",
                "dictionary_sha256",
                "pipeline_version",
            )
        },
    )
    result["text_regions"][0]["raw_text"] = "名称:Alpha"
    result["text_regions"][1]["raw_text"] = "名称:甲"
    result["ocr_fields"] = extract_fields(result["text_regions"], data)
    result["entities"] = extract_entities(result["ocr_fields"], data)
    from dataclasses import replace

    value = replace(
        value,
        input=replace(
            value.input,
            model_checksum=result["model_checksum"],
            dictionary_sha256=result["dictionary_sha256"],
        ),
    )
    result["request_hash"] = value.input.request_hash
    return value, result


def test_worker_recomputes_candidates_using_original_pinned_bytes():
    value, result = chemical_case()
    validate_result(result, value)
    assert result["entities"][0]["resolution"] == "resolved"
    assert result["outcome"] == "needs_review"


@pytest.mark.parametrize(
    "case",
    [
        "missing_context",
        "dictionary",
        "bundle",
        "threshold",
        "version",
        "candidate",
        "confidence",
        "method",
        "resolution",
        "omit",
        "duplicate",
        "facts_ready",
    ],
)
def test_worker_rejects_wrong_snapshot_or_candidates(case):
    value, result = chemical_case()
    if case == "missing_context":
        result.pop("extraction_context")
    elif case in ("dictionary", "bundle", "threshold", "version"):
        context = result["extraction_context"]
        which = "dictionary_json" if case in ("dictionary", "version") else "bundle_json"
        obj = json.loads(context[which])
        if case == "dictionary":
            obj["entries"] = []
        elif case == "threshold":
            obj["thresholds"]["entity_min"] = 0
        elif case == "version":
            obj["dictionary_version_id"] = str(uuid4())
        else:
            obj["bundle_id"] = str(uuid4())
        context[which] = json.dumps(obj)
    elif case == "omit":
        result["entities"] = []
    elif case == "duplicate":
        result["entities"].append(copy.deepcopy(result["entities"][0]))
    elif case == "facts_ready":
        result["outcome"] = "facts_ready"
    elif case == "resolution":
        result["entities"][0]["resolution"] = "unknown"
    else:
        candidate = result["entities"][0]["candidates"][0]
        name, wrong = {
            "candidate": ("canonical_name", "forged"),
            "confidence": ("confidence", 0.1),
            "method": ("match_method", "fuzzy"),
        }[case]
        candidate[name] = wrong
    with pytest.raises(InferenceInvalidResult):
        validate_result(result, value)
