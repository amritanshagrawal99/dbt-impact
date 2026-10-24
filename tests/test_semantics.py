import json
from pathlib import Path

from dbt_impact import compare, load_manifest
from dbt_impact.render import to_markdown

MANIFESTS = Path(__file__).resolve().parents[1] / "examples" / "manifests"


def manifest(tmp_path, name, snapshot_cfg, freshness, loaded_at="_loaded_at"):
    data = json.loads((MANIFESTS / "base.json").read_text())
    data["nodes"]["snapshot.shop.orders_snapshot"] = {
        "resource_type": "snapshot", "name": "orders_snapshot", "relation_name": '"shop"."snapshots"."orders_snapshot"',
        "compiled_code": 'select * from "shop"."main"."stg_orders"', "checksum": {"checksum": "x"},
        "depends_on": {"nodes": ["model.shop.stg_orders"]}, "columns": {}, "config": snapshot_cfg,
    }
    data["sources"] = {"source.shop.erp.orders": {
        "resource_type": "source", "name": "orders", "relation_name": '"erp"."raw"."orders"', "columns": {},
        "depends_on": {"nodes": []}, "freshness": freshness, "loaded_at_field": loaded_at, "config": {},
    }}
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return load_manifest(path)


SNAP = {"strategy": "timestamp", "unique_key": "order_id", "updated_at": "updated_at"}
FRESH = {"warn_after": {"count": 6, "period": "hour"}, "error_after": {"count": 24, "period": "hour"}}


def changes(rep):
    return {(c.node, c.setting, c.before, c.after) for c in rep.semantic_changes}


def test_no_semantic_changes(tmp_path):
    rep = compare(manifest(tmp_path, "a.json", SNAP, FRESH), manifest(tmp_path, "b.json", SNAP, FRESH))
    assert rep.semantic_changes == []


def test_snapshot_key_and_strategy_changes(tmp_path):
    new = {"strategy": "check", "unique_key": ["order_id", "line_id"], "check_cols": "all"}
    rep = compare(manifest(tmp_path, "a.json", SNAP, FRESH), manifest(tmp_path, "b.json", new, FRESH))
    assert changes(rep) == {
        ("orders_snapshot", "snapshot.strategy", "timestamp", "check"),
        ("orders_snapshot", "snapshot.unique_key", "order_id", "order_id, line_id"),
        ("orders_snapshot", "snapshot.updated_at", "updated_at", None),
        ("orders_snapshot", "snapshot.check_cols", None, "all"),
    }
    assert "### Behaviour changes" in to_markdown(rep)


def test_source_freshness_loosened(tmp_path):
    loose = {"warn_after": {"count": 48, "period": "hour"}}
    rep = compare(manifest(tmp_path, "a.json", SNAP, FRESH), manifest(tmp_path, "b.json", SNAP, loose, loaded_at="ingested_at"))
    assert changes(rep) == {
        ("orders", "freshness.warn_after", "6 hour", "48 hour"),
        ("orders", "freshness.error_after", "24 hour", None),
        ("orders", "freshness.loaded_at_field", "_loaded_at", "ingested_at"),
    }
    md = to_markdown(rep)
    assert "Late-data error threshold changed." in md and not rep.has_breaking  # informative, not a build break
