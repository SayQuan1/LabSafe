"""Frozen development dictionary and deterministic candidates, without safety classes."""

import hashlib
import json
from collections import defaultdict

from packages.inference_protocol.contract import validate
from packages.inference_protocol.fields import normalize, valid_cas

ALGORITHM_ID = "chemical-candidates-v1"
RUNTIME_PROFILE = "dfine-cpu-fp32-ocrv6smallcpu-chemical-v2"
MAX_BYTES = 65536
MAX_DISTANCE_CELLS = 2_000_000


def key(text):
    return normalize(text).casefold()


def dictionary(value):
    validate("DevelopmentDictionary", value)
    seen = set()
    for entry in value["entries"]:
        names = [key(entry["canonical_name"]), *map(key, entry["aliases"])]
        if (
            entry["entity_id"] in seen
            or any(not name or len(name) > 500 for name in names)
            or len(set(names)) != len(names)
            or not entry["source"].strip()
            or entry["cas"] is not None
            and not valid_cas(entry["cas"])
        ):
            raise ValueError("Invalid development dictionary")
        seen.add(entry["entity_id"])
    return value


def bounded_json(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Extraction snapshot exceeds capacity")

    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Duplicate snapshot key")
            result[name] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    json.dumps(value, allow_nan=False)
    return value


def context_for_result(result):
    context = result.get("extraction_context")
    profile = result.get("execution_identity", {}).get("runtime_profile")
    if context is None:
        if profile == RUNTIME_PROFILE:
            raise ValueError("Frozen extraction context required")
        return None
    bundle_raw, dictionary_raw = context["bundle_json"], context["dictionary_json"]
    if (
        hashlib.sha256(bundle_raw.encode()).hexdigest() != result["model_checksum"]
        or hashlib.sha256(dictionary_raw.encode()).hexdigest() != result["dictionary_sha256"]
    ):
        raise ValueError("Extraction snapshot digest mismatch")
    bundle = bounded_json(bundle_raw)
    validate("DevelopmentCPUBundle", bundle)
    value = dictionary(bounded_json(dictionary_raw))
    if (
        bundle["bundle_id"] != result["model_bundle_id"]
        or bundle["pipeline_version"] != result["pipeline_version"]
        or bundle["dictionary_version_id"] != result["dictionary_version_id"]
        or value["dictionary_version_id"] != result["dictionary_version_id"]
        or bundle["artifacts"]["dictionary"]["sha256"] != result["dictionary_sha256"]
        or bundle["runtime_profile"] != RUNTIME_PROFILE
        or profile != RUNTIME_PROFILE
        or result["execution_identity"]["purpose"] != "development"
        or result["execution_identity"]["device_profile"] != "cpu"
    ):
        raise ValueError("Extraction identity mismatch")
    return value, bundle["thresholds"]


def exact_name(text, value):
    needle = key(text)
    return any(
        needle in [key(entry["canonical_name"]), *map(key, entry["aliases"])]
        for entry in value["entries"]
    )


def distance(left, right, budget):
    budget[0] += len(left) * len(right)
    if budget[0] > MAX_DISTANCE_CELLS:
        raise ValueError("Dictionary distance budget exceeded")
    row = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        following = [i]
        for j, b in enumerate(right, 1):
            following.append(min(following[-1] + 1, row[j] + 1, row[j - 1] + (a != b)))
        row = following
    return row[-1]


def proposals(text, value, budget):
    if not text:
        return {}
    needle = key(text)
    entries = value["entries"]
    if valid_cas(needle):
        return {e["entity_id"]: (1.0, "cas_exact") for e in entries if e["cas"] == needle}
    names = {e["entity_id"]: [key(e["canonical_name"]), *map(key, e["aliases"])] for e in entries}
    exact = {
        identifier: (1.0, "alias_exact")
        for identifier, aliases in names.items()
        if needle in aliases
    }
    if exact:
        return exact
    result = {}
    for identifier, aliases in names.items():
        score = max(
            1 - distance(needle, alias, budget) / max(len(needle), len(alias)) for alias in aliases
        )
        if score > 0:
            result[identifier] = score, "fuzzy"
    return result


def extract_entities(fields, value, ocr_min=0.6, entity_min=0.9):
    """All name fields vote before top5; missing votes count zero, never disappear."""
    dictionary(value)
    if len(fields) > 500 or any(
        type(t) not in (int, float) or not 0 <= t <= 1 for t in (ocr_min, entity_min)
    ):
        raise ValueError("Invalid candidate extraction capacity or thresholds")
    groups = defaultdict(list)
    for field in fields:
        if field["field"] == "name":
            groups[field["image_id"], field["detection_id"]].append(field)
    by_id = {entry["entity_id"]: entry for entry in value["entries"]}
    budget, results = [0], []
    rank = {"cas_exact": 0, "alias_exact": 1, "fuzzy": 2}
    for (image, bottle), sources in groups.items():
        votes = [proposals(f["normalized_text"], value, budget) for f in sources]
        winners = []
        for field, vote in zip(sources, votes):
            best = max((score for score, _ in vote.values()), default=0)
            winning = [identifier for identifier, (score, _) in vote.items() if score == best]
            winners.append(
                winning[0]
                if len(winning) == 1 and field["confidence"] >= ocr_min and best >= entity_min
                else None
            )
        identifiers = set().union(*votes)
        candidates = []
        for identifier in identifiers:
            score = min(vote.get(identifier, (0, "fuzzy"))[0] for vote in votes)
            method = max(
                (vote[identifier][1] for vote in votes if identifier in vote), key=rank.get
            )
            candidates.append(
                {
                    "entity_id": identifier,
                    "canonical_name": by_id[identifier]["canonical_name"],
                    "confidence": score,
                    "match_method": method,
                }
            )
        candidates.sort(key=lambda c: (-c["confidence"], c["entity_id"]))
        resolved = bool(winners) and None not in winners and len(set(winners)) == 1
        results.append(
            {
                "image_id": image,
                "detection_id": bottle,
                "candidates": candidates[:5],
                "resolution": "resolved" if resolved else "candidate" if candidates else "unknown",
            }
        )
    if len(results) > 100:
        raise ValueError("Entity capacity exceeded")
    return results
