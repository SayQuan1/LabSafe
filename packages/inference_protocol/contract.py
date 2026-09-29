"""Validate directly against generated schemas; never duplicate wire fields."""

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any, Self

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import RootModel, model_validator

DOCUMENT = json.loads(files(__package__).joinpath("contract.json").read_text(encoding="utf-8"))
FORMAT_CHECKER = FormatChecker()
if "date-time" not in FORMAT_CHECKER.checkers:
    raise RuntimeError("Install rfc3339-validator: timestamp validation must not be skipped")


@lru_cache
def validator(name: str) -> Draft202012Validator:
    schema = {
        "$ref": f"#/components/schemas/{name}",
        "components": DOCUMENT["components"],
    }
    return Draft202012Validator(schema, format_checker=FORMAT_CHECKER)


def validate(name: str, value: Any) -> None:
    # Avoid leaking image refs, tenant IDs or raw request content in error messages.
    if next(validator(name).iter_errors(value), None) is not None:
        raise ValueError(f"Payload does not satisfy {name}")


class InferenceRequest(RootModel[dict[str, Any]]):
    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        validate("InferenceRequest", self.root)
        return self
