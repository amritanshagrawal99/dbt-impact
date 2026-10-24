"""Regenerate examples/manifests/{base,pr}.json from the example dbt project.

The "pr" state simulates a typical risky change:
  * stg_customers: first_name renamed to given_name, email dropped
  * stg_orders:    status dropped (it only reaches `customers` through `orders`' o.*)
Run from the repo root:  python examples/build_manifests.py
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PROJECT, OUT = ROOT / "shop", ROOT / "manifests"
DBT = Path(sys.executable).with_name("dbt")

PR_EDITS = {
    "models/staging/stg_customers.sql": [("    first_name,\n", "    first_name as given_name,\n"), ("    email,\n", "")],
    "models/staging/stg_orders.sql": [("    cast(order_date as date) as order_date,\n    status\n", "    cast(order_date as date) as order_date\n")],
}


def compile_to(name: str) -> None:
    subprocess.run([str(DBT), "compile", "--profiles-dir", ".", "--quiet"], cwd=PROJECT, check=True)
    data = json.loads((PROJECT / "target" / "manifest.json").read_text())
    for bulky in ("macros", "docs"):  # irrelevant to impact analysis; keeps fixtures small
        data[bulky] = {}
    for volatile in ("user_id", "invocation_id", "invocation_started_at", "generated_at", "env"):
        data["metadata"].pop(volatile, None)  # no machine identifiers; stable diffs
    text = json.dumps(data, indent=1, sort_keys=True)
    text = re.sub(r'"root_path": "[^"]*"', '"root_path": "."', text)  # no local absolute paths in fixtures
    (OUT / f"{name}.json").write_text(text + "\n")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    compile_to("base")
    originals = {p: (PROJECT / p).read_text() for p in PR_EDITS}
    try:
        for p, edits in PR_EDITS.items():
            text = originals[p]
            for old, new in edits:
                assert old in text, (p, old)
                text = text.replace(old, new)
            (PROJECT / p).write_text(text)
        compile_to("pr")
    finally:
        for p, text in originals.items():
            (PROJECT / p).write_text(text)
    shutil.rmtree(PROJECT / "target", ignore_errors=True)
    print("wrote", *(str(p) for p in sorted(OUT.glob("*.json"))))


if __name__ == "__main__":
    main()
