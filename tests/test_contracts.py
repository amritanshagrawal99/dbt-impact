import json
from pathlib import Path

from dbt_impact import compare, load_manifest
from dbt_impact.render import to_markdown

MANIFESTS = Path(__file__).resolve().parents[1] / "examples" / "manifests"
UID = "model.shop.stg_customers"


def with_contract(tmp_path, name, src, columns):
    data = json.loads((MANIFESTS / src).read_text())
    node = data["nodes"][UID]
    node["config"]["contract"] = {"enforced": True, "alias_types": True}
    node["columns"] = {c: {"name": c, "data_type": t} for c, t in columns.items()}
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return load_manifest(path)


BASE_COLS = {"customer_id": "integer", "first_name": "varchar", "last_name": "varchar", "email": "varchar", "signup_date": "date"}


def issues(rep):
    return {(c.node, c.column, c.kind) for c in rep.contract_issues}


def test_removed_contract_columns(tmp_path):
    base = with_contract(tmp_path, "b.json", "base.json", BASE_COLS)
    pr = with_contract(tmp_path, "p.json", "pr.json",
                       {"customer_id": "integer", "given_name": "varchar", "last_name": "varchar", "signup_date": "date"})
    rep = compare(base, pr)
    assert issues(rep) == {("stg_customers", "first_name", "removed"), ("stg_customers", "email", "removed")}
    assert rep.has_breaking
    md = to_markdown(rep)
    assert "### Contract violations" in md and "contract violation(s)" in md


def test_contract_out_of_sync_with_sql(tmp_path):
    base = with_contract(tmp_path, "b.json", "base.json", BASE_COLS)
    pr = with_contract(tmp_path, "p.json", "pr.json", BASE_COLS)  # contract not updated after the rename
    assert issues(compare(base, pr)) == {
        ("stg_customers", "first_name", "declared_not_produced"),
        ("stg_customers", "email", "declared_not_produced"),
        ("stg_customers", "given_name", "produced_not_declared"),
    }


def test_contract_type_change(tmp_path):
    base = with_contract(tmp_path, "b.json", "base.json", BASE_COLS)
    pr_cols = dict(BASE_COLS, customer_id="bigint")
    pr = with_contract(tmp_path, "p.json", "base.json", pr_cols)  # same SQL, only the declared type changes
    rep = compare(base, pr)
    assert [(c.column, c.kind, c.detail) for c in rep.contract_issues] == [("customer_id", "type_changed", "integer -> bigint")]


def test_no_contract_no_issues():
    rep = compare(load_manifest(MANIFESTS / "base.json"), load_manifest(MANIFESTS / "pr.json"))
    assert rep.contract_issues == []
