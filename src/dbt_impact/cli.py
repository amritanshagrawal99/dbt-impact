"""`dbt-impact --base base/manifest.json --current target/manifest.json`"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .impact import compare
from .manifest import load_manifest
from .render import to_json, to_markdown


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dbt-impact", description="Column-level impact analysis between two dbt manifests.")
    p.add_argument("--base", required=True, help="manifest.json from the base branch")
    p.add_argument("--current", required=True, help="manifest.json from the PR branch (after dbt compile)")
    p.add_argument("--base-catalog", help="optional catalog.json for the base branch (adds column types)")
    p.add_argument("--current-catalog", help="optional catalog.json for the PR branch")
    p.add_argument("--dialect", help="sqlglot dialect (default: from the manifest's adapter)")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("-o", "--output", help="write the report to this file instead of stdout")
    p.add_argument("--fail-on-breaking", action="store_true", help="exit 1 if a downstream model or exposure breaks")
    p.add_argument("--version", action="version", version=f"dbt-impact {__version__}")
    a = p.parse_args(argv)

    base = load_manifest(a.base, a.base_catalog, a.dialect)
    current = load_manifest(a.current, a.current_catalog, a.dialect)
    rep = compare(base, current)
    text = to_markdown(rep) if a.format == "markdown" else to_json(rep)
    if a.output:
        Path(a.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 1 if a.fail_on_breaking and rep.has_breaking else 0


if __name__ == "__main__":
    sys.exit(main())
