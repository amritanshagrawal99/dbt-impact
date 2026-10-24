"""Render a Report as a pull-request comment (Markdown) or JSON."""
from __future__ import annotations

import json
from dataclasses import asdict

from .impact import Report

MARKER = "<!-- dbt-impact -->"


def to_json(rep: Report) -> str:
    d = asdict(rep)
    d["has_breaking"] = rep.has_breaking
    return json.dumps(d, indent=2)


def to_markdown(rep: Report) -> str:
    lines = [MARKER, "## dbt impact analysis", ""]
    breaking, propagated = rep.breaking, [i for i in rep.impacts if i.kind == "propagates"]
    if rep.has_breaking:
        lines.append(f"**{len(breaking)} breaking reference(s)** and **{len(rep.exposures)} exposure(s) at risk**.")
    elif rep.column_changes or rep.modified_models:
        lines.append("No breaking downstream references found.")
    else:
        lines.append("No model or column changes detected.")
    lines.append("")

    summary = [("Modified", rep.modified_models), ("Added", rep.added_models), ("Removed", rep.removed_models)]
    if any(v for _, v in summary):
        lines += ["| Models | |", "|---|---|"]
        lines += [f"| {k} | {', '.join(f'`{m}`' for m in v)} |" for k, v in summary if v]
        lines.append("")

    if breaking:
        lines += ["### Breaking", "", "| Downstream model | Reads removed column | Caused by |", "|---|---|---|"]
        lines += [f"| `{i.node}` | `{i.upstream}.{i.column}` | `{i.via}` |" for i in breaking]
        lines.append("")
    if rep.exposures:
        lines += ["### Exposures at risk", "", "| Exposure | Type | Owner | Depends on | Why | Columns |", "|---|---|---|---|---|---|"]
        for e in rep.exposures:
            nodes = ", ".join(f"`{n}`" for n in e.node.split(", "))
            cols = ", ".join(f"`{c}`" for c in e.columns)
            lines.append(f"| `{e.exposure}` | {e.type} | {e.owner or '-'} | {nodes} | {e.reason} | {cols} |")
        lines.append("")
    if propagated:
        lines += ["### Propagated through `select *`", "",
                  "These models don't name the column, but it disappears from their output:", ""]
        lines += [f"- `{i.node}` loses `{i.column}` (from `{i.upstream}`)" for i in propagated]
        lines.append("")
    if rep.column_changes:
        lines += ["<details><summary>Column changes</summary>", "", "| Model | Column | Change |", "|---|---|---|"]
        lines += [f"| `{c.node}` | `{c.column}` | {c.kind}{' ' + c.detail if c.detail else ''} |" for c in rep.column_changes]
        lines += ["", "</details>", ""]
    for node, old, new in rep.rename_hints:
        lines.append(f"> Looks like `{node}.{old}` was renamed to `{new}`. Downstream models still using `{old}` will fail.")
    if rep.unanalysed:
        lines += ["", "<details><summary>Models that could not be analysed</summary>", ""]
        lines += [f"- `{k}`: {v}" for k, v in rep.unanalysed.items()]
        lines += ["", "</details>"]
    return "\n".join(lines).rstrip() + "\n"
