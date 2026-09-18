#!/usr/bin/env python3
"""Check the deploy config without a workspace.

`databricks bundle validate` resolves the current user against the workspace, so it
needs credentials CI does not have (and should not have) on a pull request. These
checks are the part that can be made locally: the three YAML files parse, the app
entrypoint is declared, and databricks.yml and target.yml.sample agree on which
variables exist and which ones a deploy has to supply.

    python .github/scripts/check_bundle_config.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
errors: list[str] = []


def load(name: str) -> dict:
    path = ROOT / name
    if not path.is_file():
        errors.append(f"{name}: missing")
        return {}
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        errors.append(f"{name}: does not parse — {exc}")
        return {}


app_yaml = load("app.yaml")
bundle = load("databricks.yml")
sample = load("target.yml.sample")

# app.yaml carries the command Databricks Apps runs.
command = app_yaml.get("command")
if not isinstance(command, list) or not command:
    errors.append("app.yaml: 'command' must be a non-empty list")

# The bundle must still declare the app this repo deploys.
apps = (bundle.get("resources") or {}).get("apps") or {}
if not apps:
    errors.append("databricks.yml: no resources.apps entry")

declared = bundle.get("variables") or {}
referenced = set(re.findall(r"\$\{var\.([A-Za-z0-9_]+)\}", (ROOT / "databricks.yml").read_text()))

for name in sorted(referenced - set(declared)):
    errors.append(f"databricks.yml: ${{var.{name}}} is used but not declared under 'variables:'")

# A variable with no default has to come from target.yml, so the committed sample
# must show it — otherwise a fresh clone copies the sample and the deploy fails on
# a missing value.
needs_value = {n for n, spec in declared.items() if not isinstance(spec, dict) or "default" not in spec}
sample_vars: set[str] = set()
for target in (sample.get("targets") or {}).values():
    sample_vars |= set((target or {}).get("variables") or {})
for name in sorted(needs_value - sample_vars):
    errors.append(
        f"target.yml.sample: '{name}' has no default in databricks.yml, so the sample must set it"
    )

for problem in errors:
    print(f"error: {problem}", file=sys.stderr)
print(
    f"checked app.yaml, databricks.yml, target.yml.sample: "
    f"{len(declared)} bundle variables, {len(needs_value)} requiring a value from target.yml"
)
sys.exit(1 if errors else 0)
