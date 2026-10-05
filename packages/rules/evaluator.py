"""Deterministic rules-dnf-v1 evaluator with explicit unknown propagation."""

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


class RuleDefinitionError(ValueError):
    pass


_FIELDS = {
    "left.storage_class": "str",
    "right.storage_class": "str",
    "same_location": "bool",
    "adjacent": "bool",
    "expiry_date": "date",
}
_EXPLANATION_PLACEHOLDERS = frozenset({"left_name", "right_name", "expiry_date", "reference_date"})
_SCOPE_RANK = {"global": 0, "tenant": 1, "laboratory": 2}


def _schema():
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "contracts" / "rule-dsl-v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate_bundle(bundle: dict[str, Any]) -> None:
    if not isinstance(bundle, dict):
        raise RuleDefinitionError("RULESET_INVALID")
    error = next(
        Draft202012Validator(_schema(), format_checker=FormatChecker()).iter_errors(bundle),
        None,
    )
    if error is not None:
        raise RuleDefinitionError("RULESET_INVALID")
    seen: set[tuple[str, str, str | None]] = set()
    for rule in bundle["rules"]:
        scope = rule["scope"]
        scope_id = rule["scope_id"]
        key = (rule["rule_id"], scope, scope_id)
        if key in seen:
            raise RuleDefinitionError("RULESET_INVALID")
        seen.add(key)
        if (scope == "global") != (scope_id is None):
            raise RuleDefinitionError("RULESET_INVALID")

        try:
            effective_from = _parse_datetime(rule["effective_from"])
            effective_to = _parse_datetime(rule["effective_to"]) if rule["effective_to"] else None
        except (TypeError, ValueError):
            raise RuleDefinitionError("RULESET_INVALID") from None
        if effective_to is not None and effective_to <= effective_from:
            raise RuleDefinitionError("RULESET_INVALID")

        case_ids = rule["case_ids"]
        if len(case_ids) != len(set(case_ids)):
            raise RuleDefinitionError("RULESET_INVALID")
        placeholders = re.findall(r"\{([^{}]*)\}", rule["explanation_template"])
        if any(value not in _EXPLANATION_PLACEHOLDERS for value in placeholders):
            raise RuleDefinitionError("RULESET_INVALID")

        for clause in rule["clauses"]:
            for atom in clause:
                _validate_atom(atom)
            if rule["finding_type"] == "incompatible_storage":
                guards = {
                    (atom["field"], atom["op"], atom.get("value"))
                    for atom in clause
                    if not isinstance(atom.get("value"), list)
                }
                if {
                    ("same_location", "eq", True),
                    ("adjacent", "eq", True),
                } - guards:
                    raise RuleDefinitionError("RULESET_INVALID")


def _parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(timezone.utc)


def _validate_atom(atom: dict[str, Any]) -> None:
    field = atom["field"]
    kind = _FIELDS[field]
    op = atom["op"]
    has_value = "value" in atom
    value = atom.get("value")
    if kind == "date":
        if op != "before_reference_date" or has_value:
            raise RuleDefinitionError("RULESET_INVALID")
        return
    if kind == "bool":
        if op != "eq" or not isinstance(value, bool):
            raise RuleDefinitionError("RULESET_INVALID")
        return
    if op == "eq":
        if not isinstance(value, str):
            raise RuleDefinitionError("RULESET_INVALID")
        return
    if op == "in":
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) for item in value)
            or len(value) != len(set(value))
        ):
            raise RuleDefinitionError("RULESET_INVALID")
        return
    raise RuleDefinitionError("RULESET_INVALID")


def _truth(value: Any, expected: Any, op: str, reference_date: str | date | None):
    if value is None:
        return None
    if op == "eq":
        if type(value) is not type(expected):
            return None
        return value == expected
    if op == "in":
        if (
            not isinstance(value, str)
            or not isinstance(expected, list)
            or any(not isinstance(item, str) for item in expected)
        ):
            return None
        return value in expected
    if op == "before_reference_date":
        if not reference_date or not isinstance(value, str):
            return None
        try:
            left = date.fromisoformat(value)
            right = (
                reference_date.date()
                if isinstance(reference_date, datetime)
                else reference_date
                if isinstance(reference_date, date)
                else date.fromisoformat(str(reference_date))
            )
        except (TypeError, ValueError):
            return None
        return left < right
    raise RuleDefinitionError("RULESET_INVALID")


def evaluate_clause(clause: list[dict[str, Any]], facts: dict[str, Any], reference_date):
    values = []
    missing = []
    for atom in clause:
        field = atom["field"]
        value = facts.get(field)
        kind = _FIELDS.get(field)
        if kind == "str" and value is not None and not isinstance(value, str):
            result = None
        elif kind == "bool" and value is not None and type(value) is not bool:
            result = None
        elif kind == "date" and value is not None and not isinstance(value, str):
            result = None
        else:
            result = _truth(value, atom.get("value"), atom["op"], reference_date)
        values.append(result)
        if result is None:
            missing.append(field)
        if result is False:
            return False, missing
    return (None if missing else True), missing


def evaluate_rule(rule: dict[str, Any], facts: dict[str, Any], reference_date):
    clause_results = []
    unknown_fields = []
    for clause in rule["clauses"]:
        result, missing = evaluate_clause(clause, facts, reference_date)
        clause_results.append(result)
        unknown_fields.extend(missing)
        if result is True:
            return True, sorted(set(unknown_fields))
    if any(result is None for result in clause_results):
        return None, sorted(set(unknown_fields))
    return False, sorted(set(unknown_fields))


def fingerprint(rule_id: str, subject_ids: list[str] | tuple[str, ...] = ()) -> str:
    ordered = [str(value) for value in subject_ids]
    payload = json.dumps([rule_id, *ordered], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _reference_at(value: str | date | datetime) -> datetime:
    if isinstance(value, datetime):
        return _parse_datetime(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value)
    try:
        return _parse_datetime(text)
    except ValueError:
        return datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=timezone.utc)


def select_rules(
    bundle: dict[str, Any],
    tenant_id: str | None,
    laboratory_id: str | None,
    reference_at: str | date | datetime,
) -> list[dict[str, Any]]:
    """Select the immutable rule applicable to each rule_id at a fixed time."""
    validate_bundle(bundle)
    at = _reference_at(reference_at)
    chosen: dict[str, dict[str, Any]] = {}
    for rule in bundle["rules"]:
        scope = rule["scope"]
        if scope == "tenant" and rule["scope_id"] != tenant_id:
            continue
        if scope == "laboratory" and rule["scope_id"] != laboratory_id:
            continue
        start = _parse_datetime(rule["effective_from"])
        end = _parse_datetime(rule["effective_to"]) if rule["effective_to"] else None
        if at < start or (end is not None and at >= end):
            continue
        previous = chosen.get(rule["rule_id"])
        rank = (_SCOPE_RANK[scope], rule["priority"])
        previous_rank = (
            (_SCOPE_RANK[previous["scope"]], previous["priority"]) if previous is not None else None
        )
        if previous is None or rank > previous_rank:
            chosen[rule["rule_id"]] = rule
    return [chosen[key] for key in sorted(chosen) if chosen[key]["enabled"]]


def _subject_ids(facts: dict[str, Any]) -> tuple[str, ...]:
    values = facts.get("subject_ids")
    if values is None:
        values = facts.get("_subject_ids")
    if isinstance(values, (list, tuple)):
        return tuple(dict.fromkeys(str(value) for value in values if value is not None))
    return ()


def normalize_facts(
    entities: Any,
    relations: Any,
    dates: Any,
    *,
    subject_ids: Any = None,
) -> dict[str, Any]:
    """Build the small rule context from a fact revision without inventing values.

    The inference protocol stores observations as arrays.  Rule atoms consume
    a deliberately narrow projection; missing or ambiguous observations stay
    ``None`` so the evaluator can propagate ``unknown``.
    """
    facts: dict[str, Any] = {
        "entities": entities,
        "relations": relations,
        "dates": dates,
    }

    def put(key: str, value: Any) -> None:
        if key in _FIELDS and key not in facts:
            facts[key] = value
        elif key in _FIELDS and facts.get(key) is None:
            facts[key] = value

    # Human revisions may already be stored as a direct mapping.  Preserve
    # unknown keys in the source fields but only project known atom fields.
    for source in (entities, relations, dates):
        if isinstance(source, dict):
            for key, value in source.items():
                if key in _FIELDS:
                    put(key, value)
                elif key in {"subject_ids", "_subject_ids"} and subject_ids is None:
                    subject_ids = value

    entity_rows = entities if isinstance(entities, list) else []
    subjects: list[str] = []
    locations: list[Any] = []
    for entity in entity_rows:
        if not isinstance(entity, dict):
            continue
        for key in ("id", "entity_id", "detection_id", "subject_id"):
            if entity.get(key) is not None:
                subjects.append(str(entity[key]))
                break
        side = entity.get("side") or entity.get("position") or entity.get("role")
        target = entity.get("storage_class")
        if target is None:
            target = entity.get("storageClass")
        if side in {"left", "right"} and target is not None:
            put(f"{side}.storage_class", target)
        if entity.get("expiry_date") is not None:
            put("expiry_date", entity["expiry_date"])
        if entity.get("location_id") is not None:
            locations.append(entity["location_id"])
        candidates = entity.get("candidates")
        if (
            isinstance(candidates, list)
            and len(candidates) == 1
            and isinstance(candidates[0], dict)
        ):
            candidate = candidates[0]
            side = side or candidate.get("side") or candidate.get("position")
            target = candidate.get("storage_class") or candidate.get("storageClass")
            if side in {"left", "right"} and target is not None:
                put(f"{side}.storage_class", target)
            if candidate.get("expiry_date") is not None:
                put("expiry_date", candidate["expiry_date"])

    relation_rows = relations if isinstance(relations, list) else []
    relation_adjacent: list[bool | None] = []
    for relation in relation_rows:
        if not isinstance(relation, dict):
            continue
        for key in ("source_detection_id", "target_detection_id", "id", "subject_id"):
            if relation.get(key) is not None:
                subjects.append(str(relation[key]))
        value = relation.get("relation")
        if value == "adjacent":
            relation_adjacent.append(True)
        elif value == "not_adjacent":
            relation_adjacent.append(False)
        elif value == "unknown":
            relation_adjacent.append(None)
        if "adjacent" in relation and isinstance(relation["adjacent"], bool):
            relation_adjacent.append(relation["adjacent"])
        for key in ("source_location_id", "target_location_id", "location_id"):
            if relation.get(key) is not None:
                locations.append(relation[key])
        if relation.get("same_location") is not None:
            put("same_location", relation["same_location"])
    if relation_adjacent:
        distinct = set(relation_adjacent)
        put("adjacent", relation_adjacent[0] if len(distinct) == 1 else None)
    if len(locations) >= 2 and "same_location" not in facts:
        put("same_location", len(set(locations)) == 1)

    date_rows = dates if isinstance(dates, list) else []
    for value in date_rows:
        if isinstance(value, str):
            put("expiry_date", value)
        elif isinstance(value, dict):
            if value.get("expiry_date") is not None:
                put("expiry_date", value["expiry_date"])
            elif value.get("kind") in {"expiry", "expiry_date"} and value.get("value") is not None:
                put("expiry_date", value["value"])

    if subject_ids is None:
        subject_ids = subjects
    if isinstance(subject_ids, (list, tuple)):
        facts["subject_ids"] = list(
            dict.fromkeys(str(value) for value in subject_ids if value is not None)
        )
    return facts


def evaluate_bundle(
    bundle: dict[str, Any],
    facts: dict[str, Any],
    reference_date,
    *,
    tenant_id: str | None = None,
    laboratory_id: str | None = None,
):
    validate_bundle(bundle)
    rules = select_rules(bundle, tenant_id, laboratory_id, reference_date)
    subjects = _subject_ids(facts)
    results = []
    for rule in sorted(rules, key=lambda value: (value["priority"], value["rule_id"])):
        truth, unknown = evaluate_rule(rule, facts, reference_date)
        results.append(
            {
                "rule_id": rule["rule_id"],
                "truth": truth,
                "unknown_fields": unknown,
                "finding_type": rule["finding_type"],
                "severity": rule["severity"],
                "action_code": rule["action_code"],
                "explanation": rule["explanation_template"],
                "source": rule["source"],
                "scope": rule["scope"],
                "scope_id": rule["scope_id"],
                "effective_from": rule["effective_from"],
                "effective_to": rule["effective_to"],
                "case_ids": list(rule["case_ids"]),
                "subject_ids": list(subjects),
                "fingerprint": fingerprint(rule["rule_id"], subjects),
            }
        )
    return results
