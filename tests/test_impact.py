import copy
import json
from pathlib import Path

import pytest

from dbt_impact import compare, load_manifest
from dbt_impact.cli import main
from dbt_impact.render import MARKER, to_json, to_markdown

MANIFESTS = Path(__file__).resolve().parents[1] / "examples" / "manifests"
BASE, PR = MANIFESTS / "base.json", MANIFESTS / "pr.json"


@pytest.fixture(scope="module")
def report():
    return compare(load_manifest(BASE), load_manifest(PR))


def test_dialect_comes_from_adapter():
    assert load_manifest(BASE).dialect == "duckdb"


def test_modified_models(report):
    assert sorted(report.modified_models) == ["stg_customers", "stg_orders"]


def test_breaking_references(report):
    got = {(i.node, i.upstream, i.column, i.via) for i in report.breaking}
    assert got == {
        ("customers", "stg_customers", "first_name", "stg_customers"),
        ("customer_contacts", "stg_customers", "email", "stg_customers"),
        ("customers", "orders", "status", "stg_orders"),  # reaches customers via orders' o.*
    }


def test_propagation_through_star(report):
    assert [(i.node, i.column) for i in report.impacts if i.kind == "propagates"] == [("orders", "status")]


def test_exposures(report):
    by_name = {e.exposure: e for e in report.exposures}
    assert by_name["crm_sync"].columns == ["email"]
    assert by_name["revenue_dashboard"].reason == "upstream model breaks"
    assert set(by_name["revenue_dashboard"].node.split(", ")) == {"customers", "orders"}


def test_rename_hint(report):
    assert report.rename_hints == [("stg_customers", "first_name", "given_name")]


def test_breakages_are_not_reported_as_analysis_failures(report):
    assert "customers" not in report.unanalysed


def test_no_changes():
    rep = compare(load_manifest(BASE), load_manifest(BASE))
    assert not rep.impacts and not rep.modified_models and not rep.has_breaking
    assert "No model or column changes detected." in to_markdown(rep)


def test_deleted_model_breaks_dependents(tmp_path):
    data = json.loads(BASE.read_text())
    cur = copy.deepcopy(data)
    del cur["nodes"]["model.shop.stg_payments"]
    path = tmp_path / "cur.json"
    path.write_text(json.dumps(cur))
    rep = compare(load_manifest(BASE), load_manifest(path))
    assert rep.removed_models == ["stg_payments"]
    assert {(i.node, i.column) for i in rep.breaking} == {("orders", "order_id"), ("orders", "amount")}


def test_type_change_from_catalog(tmp_path):
    def catalog(amount_type):
        return {"nodes": {"model.shop.stg_payments": {"columns": {
            "payment_id": {"type": "INTEGER", "index": 1}, "order_id": {"type": "INTEGER", "index": 2},
            "payment_method": {"type": "VARCHAR", "index": 3}, "amount": {"type": amount_type, "index": 4}}}}}
    (tmp_path / "a.json").write_text(json.dumps(catalog("DOUBLE")))
    (tmp_path / "b.json").write_text(json.dumps(catalog("DECIMAL(18,2)")))
    rep = compare(load_manifest(BASE, tmp_path / "a.json"), load_manifest(BASE, tmp_path / "b.json"))
    assert [(c.node, c.column, c.kind, c.detail) for c in rep.column_changes] == [
        ("stg_payments", "amount", "type_changed", "double -> decimal(18,2)")]


def test_markdown_and_json(report):
    md = to_markdown(report)
    assert md.startswith(MARKER)
    assert "**3 breaking reference(s)**" in md and "Propagated through" in md and "renamed to `given_name`" in md
    assert json.loads(to_json(report))["has_breaking"] is True


def test_cli_exit_codes(tmp_path):
    out = tmp_path / "r.md"
    assert main(["--base", str(BASE), "--current", str(PR), "-o", str(out)]) == 0
    assert out.read_text().startswith(MARKER)
    assert main(["--base", str(BASE), "--current", str(PR), "--fail-on-breaking", "-o", str(out)]) == 1
    assert main(["--base", str(BASE), "--current", str(BASE), "--fail-on-breaking", "--format", "json", "-o", str(out)]) == 0
