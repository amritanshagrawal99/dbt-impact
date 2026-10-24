"""Read the parts of a dbt manifest (and optional catalog) that impact analysis needs."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlglot import exp

RELATION_TYPES = {"model", "seed", "snapshot", "source"}


def relation_key(name: str | exp.Table, dialect: str) -> str:
    """Case-insensitive db.schema.table key for a relation name or parsed table."""
    table = name if isinstance(name, exp.Table) else exp.to_table(name, dialect=dialect)
    return ".".join(p.name.lower() for p in table.parts)


@dataclass
class Node:
    unique_id: str
    name: str
    resource_type: str
    relation: str | None
    sql: str | None
    checksum: str | None
    depends_on: list[str]
    declared_columns: dict[str, str | None]  # documented in YAML (name -> data_type)
    catalog_columns: dict[str, str] | None = None  # from catalog.json, if provided
    contract_enforced: bool = False  # config.contract.enforced: the declared columns are a public API
    semantics: dict = field(default_factory=dict)  # snapshot strategy keys, source freshness: behaviour without columns
    children: list[str] = field(default_factory=list)

    @property
    def known_columns(self) -> dict[str, str | None] | None:
        if self.catalog_columns:
            return dict(self.catalog_columns)
        return dict(self.declared_columns) or None


@dataclass
class Exposure:
    unique_id: str
    name: str
    type: str
    owner: str | None
    depends_on: list[str]


@dataclass
class Manifest:
    nodes: dict[str, Node]
    exposures: dict[str, Exposure]
    dialect: str

    def by_relation(self) -> dict[str, Node]:
        return {n.relation: n for n in self.nodes.values() if n.relation}

    def topo_order(self) -> list[Node]:
        seen: set[str] = set()
        order: list[Node] = []

        def visit(uid: str, stack: frozenset[str] = frozenset()) -> None:
            if uid in seen or uid not in self.nodes or uid in stack:
                return
            for dep in self.nodes[uid].depends_on:
                visit(dep, stack | {uid})
            seen.add(uid)
            order.append(self.nodes[uid])

        for uid in sorted(self.nodes):
            visit(uid)
        return order


_ADAPTER_DIALECT = {"databricks": "databricks", "spark": "spark", "snowflake": "snowflake", "bigquery": "bigquery",
                    "postgres": "postgres", "redshift": "redshift", "duckdb": "duckdb", "trino": "trino",
                    "sqlserver": "tsql", "fabric": "tsql", "synapse": "tsql", "clickhouse": "clickhouse", "athena": "athena"}


def load_manifest(path: str | Path, catalog: str | Path | None = None, dialect: str | None = None) -> Manifest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    adapter = (data.get("metadata") or {}).get("adapter_type") or ""
    dialect = dialect or _ADAPTER_DIALECT.get(adapter, adapter or "duckdb")

    nodes: dict[str, Node] = {}
    for section in ("nodes", "sources"):
        for uid, raw in (data.get(section) or {}).items():
            if raw.get("resource_type") not in RELATION_TYPES:
                continue
            rel = raw.get("relation_name")
            nodes[uid] = Node(
                unique_id=uid,
                name=raw.get("name", uid),
                resource_type=raw["resource_type"],
                relation=relation_key(rel, dialect) if rel else None,
                sql=raw.get("compiled_code") if raw["resource_type"] in ("model", "snapshot") else None,
                checksum=(raw.get("checksum") or {}).get("checksum"),
                depends_on=[d for d in (raw.get("depends_on") or {}).get("nodes", [])],
                declared_columns={c.lower(): (v or {}).get("data_type") for c, v in (raw.get("columns") or {}).items()},
                contract_enforced=bool(((raw.get("config") or {}).get("contract") or {}).get("enforced")),
                semantics=_semantics(raw),
            )
    for uid, raw in (data.get("nodes") or {}).items():
        if raw.get("resource_type") == "seed" and uid in nodes and not nodes[uid].declared_columns:
            nodes[uid].declared_columns = _seed_header(raw)
    for n in nodes.values():
        for dep in n.depends_on:
            if dep in nodes:
                nodes[dep].children.append(n.unique_id)

    exposures = {}
    for uid, raw in (data.get("exposures") or {}).items():
        owner = raw.get("owner") or {}
        exposures[uid] = Exposure(uid, raw.get("name", uid), raw.get("type", ""), owner.get("name") or owner.get("email"),
                                  list((raw.get("depends_on") or {}).get("nodes", [])))

    if catalog:
        _apply_catalog(nodes, json.loads(Path(catalog).read_text(encoding="utf-8")))
    return Manifest(nodes, exposures, dialect)


SNAPSHOT_KEYS = ("strategy", "unique_key", "updated_at", "check_cols", "invalidate_hard_deletes", "hard_deletes")


def _semantics(raw: dict) -> dict:
    cfg = raw.get("config") or {}
    out = {}
    if raw.get("resource_type") == "snapshot":
        for k in SNAPSHOT_KEYS:
            v = cfg.get(k)
            if v is not None:
                out[f"snapshot.{k}"] = ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else v
    if raw.get("resource_type") == "source":
        fresh = raw.get("freshness") or {}
        for level in ("warn_after", "error_after"):
            rule = fresh.get(level) or {}
            if rule.get("count") is not None:
                out[f"freshness.{level}"] = f"{rule['count']} {rule.get('period', '')}".strip()
        if raw.get("loaded_at_field"):
            out["freshness.loaded_at_field"] = raw["loaded_at_field"]
    return out


def _seed_header(raw: dict) -> dict[str, str | None]:
    """Seeds carry no column list in the manifest; read the CSV header when the project is on disk."""
    path = Path(raw.get("root_path") or "") / (raw.get("original_file_path") or "")
    try:
        with path.open(encoding="utf-8") as fh:
            header = fh.readline()
    except OSError:
        return {}
    return {c.strip().strip('"').lower(): None for c in header.split(",") if c.strip()}


def _apply_catalog(nodes: dict[str, Node], cat: dict) -> None:
    for section in ("nodes", "sources"):
        for uid, raw in (cat.get(section) or {}).items():
            if uid in nodes:
                cols = raw.get("columns") or {}
                nodes[uid].catalog_columns = {
                    name.lower(): (c.get("type") or "").lower() for name, c in sorted(cols.items(), key=lambda kv: kv[1].get("index", 0))
                }
