"""Golden lexical cases and independently recomputed, complete field evidence."""

import copy

import pytest

from packages.domain.inference_execution import InferenceInvalidResult, validate_result
from packages.inference_protocol.fields import extract_fields, strict_date, valid_cas
from tests.evidence_helpers import add_bottles, add_regions, lease, result_for


def rows(*texts):
    return [
        {
            "image_id": "image",
            "detection_id": "bottle",
            "line_id": f"line-{i}",
            "crop_id": f"crop-{i}",
            "raw_text": text,
            "confidence": 0.9 if i == 0 else 0.3,
        }
        for i, text in enumerate(texts)
    ]


@pytest.mark.parametrize(
    "text,kind,value",
    [
        (" ＮＡＭＥ： Ethanol  ", "name", "Ethanol"),
        ("化学品名称乙醇", "name", "乙醇"),
        ("品名: 乙醇\t ETHANOL", "name", "乙醇 ETHANOL"),
        ("名称：甲", "name", "甲"),
        ("name乙醇", "name", "乙醇"),
        ("CAS:64-17-5", "name", "64-17-5"),
        ("cas 64-17-6", "name", None),
        ("64-17-5", "name", "64-17-5"),
        ("生产日期:2026-03-01", "production", "2026-03-01"),
        ("生产2026年03月01日", "production", "2026-03-01"),
        ("mfg:2024/02/29", "production", "2024-02-29"),
        ("有效期2027/03/01", "expiry", "2027-03-01"),
        ("失效:2027-03-01", "expiry", "2027-03-01"),
        ("EXP 2027-03-01", "expiry", "2027-03-01"),
        ("开封日期2026年03月01日", "opened", "2026-03-01"),
        ("开封:2026-03-01", "opened", "2026-03-01"),
        ("浓度:９５％", "concentration", "95%"),
        ("含量 5 mol/L", "concentration", "5 mol/L"),
        ("concentration: 2%", "concentration", "2%"),
        ("危险标识：易燃", "hazard_mark", "易燃"),
        ("危险性 腐蚀", "hazard_mark", "腐蚀"),
        ("hazard: flammable", "hazard_mark", "flammable"),
        ("生产日期:2026-03-01 有效期:2027-03-01", "date_unknown", None),
        ("EXP MFG", "date_unknown", None),
        ("2027-03-01", "date_unknown", None),
        ("打印日期 2027-03", "date_unknown", None),
        ("10/11/2027", "date_unknown", None),
        ("EXP", "expiry", None),
        ("CAS", "name", None),
    ],
)
def test_single_line_golden_preserves_original(text, kind, value):
    sources = rows(text)
    field = extract_fields(sources)[0]
    assert (field["field"], field["normalized_text"]) == (kind, value)
    assert field["raw_text"] == text
    assert field["source_lines"] == [
        {key: sources[0][key] for key in ("line_id", "crop_id", "raw_text", "confidence")}
    ]


@pytest.mark.parametrize("text", ["Ethanol", "乙醇", "NAMEthanol", "EXPIRED", "64-17-6", "", "  "])
def test_no_unfrozen_dictionary_or_english_prefix_guess(text):
    assert extract_fields(rows(text)) == []


@pytest.mark.parametrize(
    "text",
    [
        "2027-03",
        "2027-02-30",
        "2026-02-29",
        "10/11/2027",
        "2027-3-01",
        "2027-03-1",
        "2027-03-01 extra",
        "0000-01-01",
        "2027/03-01",
    ],
)
def test_entire_strict_date_value(text):
    assert strict_date(text) is None
    assert extract_fields(rows("EXP:" + text))[0]["normalized_text"] is None


@pytest.mark.parametrize(
    "text,valid",
    [
        ("64-17-5", True),
        ("7732-18-5", True),
        ("50-00-0", True),
        ("64-17-6", False),
        ("6-17-5", False),
        ("12345678-12-3", False),
        ("64-1-5", False),
        ("64-17-55", False),
        ("64-17-5 extra", False),
    ],
)
def test_cas_complete_format_and_checksum(text, valid):
    assert valid_cas(text) is valid


def test_pair_sources_low_confidence_and_conflicts_all_preserved():
    source = rows("ＮＡＭＥ", " Ethanol ", "有效期", "2027/03/01", "有效期:2027-04-01")
    fields = extract_fields(source)
    assert [(f["field"], f["normalized_text"]) for f in fields] == [
        ("name", "Ethanol"),
        ("expiry", "2027-03-01"),
        ("expiry", "2027-04-01"),
    ]
    assert fields[0]["raw_text"] == "ＮＡＭＥ\n Ethanol "
    assert fields[0]["confidence"] == 0.3
    assert [s["crop_id"] for s in fields[0]["source_lines"]] == ["crop-0", "crop-1"]
    assert fields[0]["crop_id"] == "crop-0"
    assert source == rows("ＮＡＭＥ", " Ethanol ", "有效期", "2027/03/01", "有效期:2027-04-01")


@pytest.mark.parametrize("block", ["unknown", "other_bottle", "other_image", "blank", "prefix"])
def test_global_adjacency_never_filters_intervening_rows(block):
    source = rows("有效期", "noise", "2027-03-01")
    if block == "unknown":
        source[1]["detection_id"] = None
    elif block == "other_bottle":
        source[1]["detection_id"] = "other"
    elif block == "other_image":
        source[1]["image_id"] = "other"
    elif block == "blank":
        source[1]["raw_text"] = " \t "
    else:
        source[1]["raw_text"] = "生产日期:2026-03-01"
    fields = extract_fields(source)
    assert fields[0]["field"] == "expiry" and fields[0]["normalized_text"] is None
    assert len(fields[0]["source_lines"]) == 1
    assert fields[-1]["field"] == "date_unknown"
    if block == "prefix":
        assert fields[1]["field"] == "production"


def test_inline_value_never_appends_following_line():
    fields = extract_fields(rows("NAME:甲", "乙", "EXP:2027-03-01", "2027-04-01"))
    assert fields[0]["normalized_text"] == "甲"
    assert len(fields[0]["source_lines"]) == 1
    assert [f["field"] for f in fields] == ["name", "expiry", "date_unknown"]


def test_capacity_boundaries_unicode_and_unknown_lines():
    assert extract_fields(rows("名称:" + "甲" * 500))[0]["normalized_text"] == "甲" * 500
    source = rows("NAME" + " " * 1495, "乙" * 500)
    assert len(extract_fields(source)[0]["raw_text"]) == 2000
    assert len(extract_fields(rows(*(["名称:甲"] * 100)))) == 100
    source = rows("名称:甲")
    source[0]["detection_id"] = None
    assert extract_fields(source) == []


def test_english_keyword_inside_word_is_not_a_date_keyword():
    field = extract_fields(rows("EXP:2027-03-01 PREMFG"))[0]
    assert field["field"] == "expiry" and field["normalized_text"] is None


def test_ambiguous_date_line_cannot_be_consumed_as_a_name_value():
    fields = extract_fields(rows("NAME", "printed MFG 2026-03-01 EXP 2027-03-01"))
    assert [(f["field"], f["normalized_text"]) for f in fields] == [
        ("name", None),
        ("date_unknown", None),
    ]


@pytest.mark.parametrize(
    "source",
    [
        rows("x" * 2001),
        rows("NAME:" + "甲" * 501),
        rows("NAME" + " " * 1500, "甲" * 500),
        rows(*(["名称:甲"] * 101)),
    ],
)
def test_capacity_rejects_without_truncation(source):
    with pytest.raises(ValueError):
        extract_fields(source)


def field_case():
    value = lease()
    result = add_bottles(value, add_regions(value, result_for(value, "needs_review")))
    result["text_regions"][0].update(raw_text="NAME", confidence=0.9)
    result["text_regions"][1].update(raw_text="Ethanol", confidence=0.3)
    result["ocr_fields"] = extract_fields(result["text_regions"])
    return value, result


def test_worker_accepts_complete_multiline_low_confidence():
    value, result = field_case()
    validate_result(result, value)
    assert result["outcome"] == "needs_review" and not result["entities"]


@pytest.mark.parametrize(
    "mutation",
    [
        "omit",
        "duplicate",
        "single_crop",
        "wrong_crop",
        "wrong_line",
        "source_raw",
        "source_confidence",
        "raw",
        "confidence",
        "normalized",
        "kind",
        "bottle",
        "first_crop",
        "reverse",
    ],
)
def test_worker_rejects_forged_missing_or_partial_field_sources(mutation):
    value, result = field_case()
    field = result["ocr_fields"][0]
    if mutation == "omit":
        result["ocr_fields"] = []
    elif mutation == "duplicate":
        result["ocr_fields"].append(copy.deepcopy(field))
    elif mutation == "single_crop":
        field["source_lines"].pop()
    elif mutation in ("wrong_crop", "wrong_line"):
        key = "crop_id" if mutation == "wrong_crop" else "line_id"
        field["source_lines"][1][key] = field["source_lines"][0][key]
    elif mutation in ("source_raw", "source_confidence"):
        field["source_lines"][1]["raw_text" if mutation == "source_raw" else "confidence"] = (
            "forged" if mutation == "source_raw" else 1
        )
    elif mutation == "reverse":
        field["source_lines"].reverse()
    else:
        key, wrong = {
            "raw": ("raw_text", "NAME\nforged"),
            "confidence": ("confidence", 1),
            "normalized": ("normalized_text", "forged"),
            "kind": ("field", "expiry"),
            "bottle": ("detection_id", value.input.images[0].image_id),
            "first_crop": ("crop_id", result["crops"][1]["crop_id"]),
        }[mutation]
        field[key] = wrong
    with pytest.raises(InferenceInvalidResult):
        validate_result(result, value)
