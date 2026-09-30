"""Synthetic DDL-only fixtures; not application repositories or production seed data."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import text

from packages.persistence.schema import model

TABLES = model()["tables"]


def insert(connection, table, **values):
    definition = TABLES[table]
    for name, column in definition["columns"].items():
        if (
            name in values
            or column["nullable"]
            or column["default_sql"] is not None
            or column["generated_sql"]
        ):
            continue
        kind = column["type"]
        if kind == "CHAR(36)":
            values[name] = str(uuid4())
        elif kind == "CHAR(64)":
            values[name] = uuid4().hex * 2
        elif kind.startswith("VARCHAR"):
            values[name] = uuid4().hex
        elif kind == "JSON":
            values[name] = "{}"
        elif kind.startswith("DATETIME"):
            values[name] = datetime(2026, 9, 29)
        elif kind == "DATE":
            values[name] = datetime(2026, 9, 29).date()
        elif "INT" in kind:
            values[name] = 1
        else:
            raise AssertionError(f"No fixture value for {table}.{name}")
    assert set(values) <= definition["columns"].keys()
    quote = chr(96)
    fields = ",".join(quote + name + quote for name in values)
    parameters = ",".join(":" + name for name in values)
    connection.execute(
        text(f"INSERT INTO {quote}{table}{quote} ({fields}) VALUES ({parameters})"), values
    )
    return values["id"]


class Graph:
    def __init__(self, connection):
        self.connection = connection
        self.tenant = insert(connection, "tenants", timezone="UTC")
        self.user = self.add("users")
        self.college = self.add("colleges")
        self.lab = self.add("laboratories", college_id=self.college)
        self.location = self.add("locations", laboratory_id=self.lab)
        self.template = self.add("inspection_templates")
        self.inspection = self.add(
            "inspections", laboratory_id=self.lab, template_id=self.template, inspector_id=self.user
        )
        self.dictionary = self.add("dictionary_versions")
        self.model = self.add("model_versions", dictionary_version_id=self.dictionary)
        self.rules = self.add("rule_bundles")
        self.item, self.run, self.fact, self.evaluation = self.item_chain()
        self.other_item, self.other_run, self.other_fact, self.other_evaluation = self.item_chain()
        self.finding = self.add(
            "findings",
            item_id=self.item,
            run_id=self.run,
            fact_revision_id=self.fact,
            rule_evaluation_id=self.evaluation,
        )
        self.task = self.add(
            "remediation_tasks",
            finding_id=self.finding,
            laboratory_id=self.lab,
            assignee_id=self.user,
        )
        self.upload = self.add(
            "uploads", laboratory_id=self.lab, inspection_item_id=self.item, requested_by=self.user
        )
        self.image = self.add(
            "asset_images",
            laboratory_id=self.lab,
            inspection_item_id=self.item,
            upload_id=self.upload,
        )
        self.add("run_images", item_id=self.item, run_id=self.run, image_id=self.image)

    def add(self, table, **values):
        return insert(self.connection, table, tenant_id=self.tenant, **values)

    def item_chain(self):
        template_item = self.add("template_items", template_id=self.template)
        item = self.add(
            "inspection_items",
            inspection_id=self.inspection,
            laboratory_id=self.lab,
            template_item_id=template_item,
            location_id=self.location,
        )
        run = self.add(
            "inference_runs",
            item_id=item,
            laboratory_id=self.lab,
            model_bundle_id=self.model,
            dictionary_version_id=self.dictionary,
            rule_bundle_id=self.rules,
        )
        fact = self.add("fact_revisions", item_id=item, run_id=run)
        evaluation = self.add(
            "rule_evaluations",
            item_id=item,
            run_id=run,
            fact_revision_id=fact,
            rule_bundle_id=self.rules,
        )
        return item, run, fact, evaluation
