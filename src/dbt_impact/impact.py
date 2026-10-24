"""Compare base and current manifests and trace column changes to everything downstream."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .lineage import ModelFacts, analyse_manifest
from .manifest import Manifest, Node


@dataclass
class ColumnChange:
    node: str
    column: str
    kind: str  # removed | added | type_changed
    detail: str = ""


@dataclass
class Impact:
    node: str
    upstream: str
    column: str
    kind: str  # breaks | propagates
    via: str   # node whose change caused it (the original changed model)


@dataclass
class ExposureRisk:
    exposure: str
    type: str
    owner: str | None
    node: str
    columns: list[str]
    reason: str  # "columns removed" | "upstream model breaks"


@dataclass
class Report:
    added_models: list[str] = field(default_factory=list)
    removed_models: list[str] = field(default_factory=list)
    modified_models: list[str] = field(default_factory=list)
    column_changes: list[ColumnChange] = field(default_factory=list)
    rename_hints: list[tuple[str, str, str]] = field(default_factory=list)  # (node, old, new)
    impacts: list[Impact] = field(default_factory=list)
    exposures: list[ExposureRisk] = field(default_factory=list)
    unanalysed: dict[str, str] = field(default_factory=dict)

    @property
    def breaking(self) -> list[Impact]:
        return [i for i in self.impacts if i.kind == "breaks"]

    @property
    def has_breaking(self) -> bool:
        return bool(self.breaking or self.exposures)


def _outputs(node: Node, facts: dict[str, ModelFacts]) -> list[str] | None:
    f = facts.get(node.unique_id)
    if f and f.outputs:
        return f.outputs
    cols = node.known_columns
    return list(cols) if cols else None


def _types(node: Node) -> dict[str, str]:
    return {k: v for k, v in (node.known_columns or {}).items() if v}


def compare(base: Manifest, current: Manifest) -> Report:
    rep = Report()
    base_facts, cur_facts = analyse_manifest(base), analyse_manifest(current)
    for uid, f in cur_facts.items():
        if f.error:
            rep.unanalysed[current.nodes[uid].name] = f.error

    removed_cols: list[tuple[Node, str]] = []  # (node in base, column) to trace downstream
    for uid, node in base.nodes.items():
        if node.resource_type == "model" and uid not in current.nodes:
            rep.removed_models.append(node.name)
            for col in _outputs(node, base_facts) or []:
                removed_cols.append((node, col))

    for uid, node in current.nodes.items():
        if node.resource_type not in ("model", "snapshot", "seed", "source"):
            continue
        old = base.nodes.get(uid)
        if old is None:
            if node.resource_type == "model":
                rep.added_models.append(node.name)
            continue
        if node.resource_type == "model" and old.checksum != node.checksum:
            rep.modified_models.append(node.name)
        before, after = _outputs(old, base_facts), _outputs(node, cur_facts)
        if before is None or after is None:
            continue
        gone = [c for c in before if c not in after]
        new = [c for c in after if c not in before]
        rep.column_changes += [ColumnChange(node.name, c, "removed") for c in gone]
        rep.column_changes += [ColumnChange(node.name, c, "added") for c in new]
        old_t, new_t = _types(old), _types(node)
        for c in after:
            if c in old_t and c in new_t and old_t[c] != new_t[c]:
                rep.column_changes.append(ColumnChange(node.name, c, "type_changed", f"{old_t[c]} -> {new_t[c]}"))
        aliases = cur_facts[uid].aliases if uid in cur_facts else {}
        for c in new:
            if aliases.get(c) in gone:
                rep.rename_hints.append((node.name, aliases[c], c))
        removed_cols += [(old, c) for c in gone]

    # Trace from the most upstream change first, so propagated removals are credited to their origin.
    order = {n.unique_id: i for i, n in enumerate(base.topo_order())}
    removed_cols.sort(key=lambda nc: order.get(nc[0].unique_id, len(order)))
    _trace(removed_cols, current, cur_facts, rep)
    broken = {i.node for i in rep.breaking}
    rep.unanalysed = {k: v for k, v in rep.unanalysed.items() if k not in broken}
    return rep


def _trace(removed: list[tuple[Node, str]], current: Manifest, facts: dict[str, ModelFacts], rep: Report) -> None:
    queue = deque((n.unique_id, n.relation, c, n.name) for n, c in removed if n.relation)
    seen: set[tuple[str, str]] = set()
    risky: dict[str, set[str]] = {}
    while queue:
        uid, relation, col, origin = queue.popleft()
        if (uid, col) in seen:
            continue
        seen.add((uid, col))
        risky.setdefault(uid, set()).add(col)
        parent_name = current.nodes[uid].name if uid in current.nodes else uid.split(".")[-1]
        for child in _children(uid, relation, current):
            f = facts.get(child.unique_id)
            if f is None:
                continue
            if col in f.explicit.get(relation, set()):
                rep.impacts.append(Impact(child.name, parent_name, col, "breaks", origin))
            elif relation in f.star_from:
                rep.impacts.append(Impact(child.name, parent_name, col, "propagates", origin))
                queue.appendleft((child.unique_id, child.relation, col, origin))  # credit the true origin
    broken: dict[str, set[str]] = {}
    for i in rep.breaking:
        broken.setdefault(i.node, set()).add(i.column)
    for exp_ in current.exposures.values():
        nodes, cols, reasons = [], set(), set()
        for dep in exp_.depends_on:
            name = current.nodes[dep].name if dep in current.nodes else dep.split(".")[-1]
            if name in broken:
                nodes.append(name); cols |= broken[name]; reasons.add("upstream model breaks")
            elif dep in risky:
                nodes.append(name); cols |= risky[dep]; reasons.add("columns removed")
        if nodes:
            rep.exposures.append(ExposureRisk(exp_.name, exp_.type, exp_.owner, ", ".join(nodes), sorted(cols),
                                              "upstream model breaks" if "upstream model breaks" in reasons else "columns removed"))


def _children(uid: str, relation: str, current: Manifest) -> list[Node]:
    if uid in current.nodes:
        return [current.nodes[c] for c in current.nodes[uid].children]
    # Model was deleted: anything still depending on it, or reading its relation, is affected.
    return [n for n in current.nodes.values() if uid in n.depends_on or (n.sql and relation in (n.sql or "").lower())]
