"""Deterministic OCR lexing, shared by CPU and Worker; no chemical inference."""

import re
import unicodedata
from datetime import date

ALGORITHM_ID = "ocr-fields-v1"
PREFIXES = {
    "name": ("化学品名称", "名称", "品名", "NAME", "CAS"),
    "expiry": ("有效期", "失效", "EXP"),
    "production": ("生产日期", "生产", "MFG"),
    "opened": ("开封日期", "开封"),
    "concentration": ("浓度", "含量", "CONCENTRATION"),
    "hazard_mark": ("危险标识", "危险性", "HAZARD"),
}
ORDERED = sorted(
    ((word, kind) for kind, words in PREFIXES.items() for word in words),
    key=lambda pair: -len(pair[0]),
)
DATE_WORDS = re.compile(
    "|".join(
        (r"(?<![a-z])" if word.isascii() else "")
        + re.escape(word)
        + (r"(?![a-z])" if word.isascii() else "")
        for word, kind in ORDERED
        if kind in ("expiry", "production", "opened")
    ),
    re.IGNORECASE,
)
DATE_VALUE = re.compile(
    r"([0-9]{4})(?:-([0-9]{2})-([0-9]{2})|/([0-9]{2})/([0-9]{2})|年([0-9]{2})月([0-9]{2})日)"
)
DATE_HINT = re.compile(r"[0-9]{4}[-/年][0-9]{1,2}|[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}")
CAS_VALUE = re.compile(r"[0-9]{2,7}-[0-9]{2}-[0-9]")


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).split())


def prefix(text):
    for word, kind in ORDERED:
        if text[: len(word)].casefold() != word.casefold():
            continue
        rest = text[len(word) :]
        if word.isascii() and rest and rest[0].isascii() and rest[0].isalpha():
            continue
        return kind, word, rest.lstrip().removeprefix(":").strip()
    return None


def valid_cas(text):
    if not CAS_VALUE.fullmatch(text):
        return False
    digits = text.replace("-", "")
    return sum(int(n) * i for i, n in enumerate(reversed(digits[:-1]), 1)) % 10 == int(digits[-1])


def strict_date(text):
    match = DATE_VALUE.fullmatch(text)
    if match is None:
        return None
    year, *parts = match.groups()
    month, day = [part for part in parts if part is not None]
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def extract_fields(lines, dictionary=None):
    """Consume global OCR order, without filtering unknown/other-bottle/blank lines."""
    if len(lines) > 100 or any(len(line["raw_text"]) > 2000 for line in lines):
        raise ValueError("OCR input capacity exceeded")
    fields, index = [], 0
    while index < len(lines):
        first = lines[index]
        text = normalize(first["raw_text"])
        index += 1
        if first["detection_id"] is None or not text:
            continue
        sources = [first]
        found = prefix(text)
        if len(DATE_WORDS.findall(text)) >= 2:
            kind, value = "date_unknown", None
        elif found:
            kind, word, value = found
            if not value and index < len(lines):
                second = lines[index]
                following = normalize(second["raw_text"])
                if (
                    following
                    and second["image_id"] == first["image_id"]
                    and second["detection_id"] == first["detection_id"]
                    and prefix(following) is None
                    and len(DATE_WORDS.findall(following)) < 2
                ):
                    sources.append(second)
                    value = following
                    index += 1
            if kind in ("expiry", "production", "opened"):
                value = strict_date(value)
            elif word == "CAS":
                value = value if valid_cas(value) else None
            else:
                value = value or None
        elif valid_cas(text):
            kind, value = "name", text
        elif dictionary is not None and _exact_name(text, dictionary):
            kind, value = "name", text
        elif DATE_HINT.search(text):
            kind, value = "date_unknown", None
        else:
            continue
        raw = "\n".join(line["raw_text"] for line in sources)
        if len(raw) > 2000 or (value is not None and len(value) > 500):
            raise ValueError("OCR field capacity exceeded")
        fields.append(
            {
                "image_id": first["image_id"],
                "detection_id": first["detection_id"],
                "crop_id": first["crop_id"],
                "source_lines": [
                    {key: line[key] for key in ("line_id", "crop_id", "raw_text", "confidence")}
                    for line in sources
                ],
                "field": kind,
                "raw_text": raw,
                "normalized_text": value,
                "confidence": min(line["confidence"] for line in sources),
            }
        )
        if len(fields) > 500:
            raise ValueError("OCR field count exceeded")
    return fields


def _exact_name(text, dictionary):
    from packages.inference_protocol.chemical import exact_name

    return exact_name(text, dictionary)
