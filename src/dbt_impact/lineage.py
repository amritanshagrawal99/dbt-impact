"""Column-level facts about each model, derived from its compiled SQL with sqlglot.

For every model we compute:
  * outputs   - its output column names (stars expanded against upstream schemas)
  * explicit  - upstream columns it names directly (anywhere: SELECT, JOIN, WHERE, GROUP BY ...)
  * star_from - upstream relations whose columns flow out of it through `*` / `alias.*`

The difference matters: removing an upstream column that a model names explicitly breaks that
model; removing one that only reaches it through a star silently removes the column from its output,
so the change propagates further downstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from .manifest import Manifest, Node, relation_key

UNKNOWN_TYPE = "UNKNOWN"


@dataclass
class ModelFacts:
    outputs: list[str] | None  # None = could not be determined
    explicit: dict[str, set[str]] = field(default_factory=dict)
    star_from: set[str] = field(default_factory=set)
    aliases: dict[str, str] = field(default_factory=dict)  # output name -> upstream column it plainly renames
    error: str | None = None


def _schema_dict(columns_by_relation: dict[str, list[str]]) -> dict:
    out: dict = {}
    for rel, cols in columns_by_relation.items():
        parts = rel.split(".")
        cursor = out
        for p in parts[:-1]:
            cursor = cursor.setdefault(p, {})
        cursor[parts[-1]] = {c: UNKNOWN_TYPE for c in cols}
    return out


def _uniform_depth(columns_by_relation: dict[str, list[str]]) -> dict[str, list[str]]:
    # MappingSchema requires every table at the same nesting depth.
    if not columns_by_relation:
        return {}
    depth = max(len(k.split(".")) for k in columns_by_relation)
    return {k: v for k, v in columns_by_relation.items() if len(k.split(".")) == depth}


def analyse_model(sql: str, upstream_columns: dict[str, list[str]], dialect: str) -> ModelFacts:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception as e:  # noqa: BLE001 - any parse failure just makes this model opaque
        return ModelFacts(None, error=f"parse: {str(e).splitlines()[0]}")
    schema = _schema_dict(_uniform_depth(upstream_columns))
    opts = {"schema": schema, "dialect": dialect, "validate_qualify_columns": False, "quote_identifiers": False,
            "identify": False}
    try:
        unexpanded = qualify(tree.copy(), expand_stars=False, **opts)
        expanded = qualify(tree.copy(), expand_stars=True, **opts)
        error = None
    except Exception as e:  # noqa: BLE001
        # Usually the very case we care about: the model reads a column its upstream no longer has.
        # Re-qualify without a schema so explicit references are still collected.
        error = f"qualify: {str(e).splitlines()[0]}"
        try:
            unexpanded = qualify(tree.copy(), expand_stars=False, **{**opts, "schema": None})
            expanded = None
        except Exception:  # noqa: BLE001
            return ModelFacts(None, error=error)

    outputs = _outputs(expanded) if expanded is not None else None
    facts = ModelFacts(outputs=outputs or _outputs(unexpanded), error=error)
    if isinstance(unexpanded, exp.Query):
        for sel in unexpanded.selects:
            if isinstance(sel, exp.Alias) and isinstance(sel.this, exp.Column) and sel.alias.lower() != sel.this.name.lower():
                facts.aliases[sel.alias.lower()] = sel.this.name.lower()
    for scope in traverse_scope(unexpanded):
        for col in scope.columns:
            for rel, name in _resolve(scope, col.table, col.name, dialect):
                facts.explicit.setdefault(rel, set()).add(name)
        if scope.is_root:
            facts.star_from |= _star_relations(scope, dialect)
    return facts


def _outputs(tree: exp.Expression) -> list[str] | None:
    if not isinstance(tree, exp.Query):
        return None
    names = [s.alias_or_name.lower() for s in tree.selects]
    return None if any(n in ("", "*") for n in names) else names


def _stars(scope: Scope) -> list[exp.Expression]:
    """Top-level `*` and `alias.*` projections (Scope.stars misses a bare `*`)."""
    if not isinstance(scope.expression, exp.Select):
        return []
    return [s for s in scope.expression.selects
            if isinstance(s, exp.Star) or (isinstance(s, exp.Column) and isinstance(s.this, exp.Star))]


def _branches(scope: Scope) -> list[Scope]:
    """The SELECT scopes behind a scope: itself, or every branch of a UNION / INTERSECT / EXCEPT."""
    if scope.set_operation_scopes:
        return [b for u in scope.set_operation_scopes for b in _branches(u)]
    return [scope]


def _star_relations(scope: Scope, dialect: str) -> set[str]:
    rels: set[str] = set()
    for branch in _branches(scope):
        for star in _stars(branch):
            alias = star.table if isinstance(star, exp.Column) else None
            sources = [branch.sources.get(alias)] if alias else list(branch.sources.values())
            for src in sources:
                rels |= _star_source_relations(src, dialect)
    return rels


def _star_source_relations(src, dialect: str) -> set[str]:
    if isinstance(src, exp.Table):
        return {relation_key(src, dialect)}
    if isinstance(src, Scope):
        # alias.* over a CTE: columns the CTE passes through by star also flow out.
        return _star_relations(src, dialect)
    return set()


def _resolve(scope: Scope, alias: str, name: str, dialect: str, depth: int = 0) -> list[tuple[str, str]]:
    """Map a column reference to the upstream (relation, column) pairs it reads."""
    if depth > 20:
        return []
    if alias:
        src = scope.sources.get(alias)
    elif len(scope.sources) == 1:
        # Unqualified column that qualify could not place (unknown to the schema): single source owns it.
        src = next(iter(scope.sources.values()))
    else:
        src = None
    if isinstance(src, exp.Table):
        return [(relation_key(src, dialect), name.lower())]
    if isinstance(src, Scope):
        out: list[tuple[str, str]] = []
        for branch in _branches(src):
            # Named in the branch's own SELECT -> that scope already recorded its inputs.
            if any(s.alias_or_name.lower() == name.lower() for s in branch.expression.selects if not isinstance(s, exp.Star)):
                continue
            for star in _stars(branch):
                star_alias = star.table if isinstance(star, exp.Column) else None
                aliases = [star_alias] if star_alias else list(branch.sources)
                for a in aliases:
                    out += _resolve(branch, a, name, dialect, depth + 1)
        return out
    return []


def analyse_manifest(manifest: Manifest) -> dict[str, ModelFacts]:
    """Analyse every model in dependency order, feeding inferred outputs to downstream models."""
    columns: dict[str, list[str]] = {}
    facts: dict[str, ModelFacts] = {}
    for node in manifest.topo_order():
        if node.sql:
            upstream = {manifest.nodes[d].relation: columns[manifest.nodes[d].relation]
                        for d in node.depends_on
                        if d in manifest.nodes and manifest.nodes[d].relation in columns}
            f = analyse_model(node.sql, upstream, manifest.dialect)
            facts[node.unique_id] = f
            outputs = f.outputs or _known(node)
        else:
            outputs = _known(node)
        if node.relation and outputs:
            columns[node.relation] = outputs
    return facts


def _known(node: Node) -> list[str] | None:
    cols = node.known_columns
    return list(cols) if cols else None
