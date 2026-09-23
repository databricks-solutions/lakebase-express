#!/usr/bin/env python3
"""Check the deploy config without a workspace.

`databricks bundle validate` resolves the current user against the workspace, so it
needs credentials CI does not have (and should not have) on a pull request. These
checks are the part that can be made locally: the three YAML files parse, the app
entrypoint is declared, databricks.yml and target.yml.sample agree on which
variables exist and which ones a deploy has to supply, and the requirements.txt the
app installs is fully hash-pinned and identical to the lock CI tests.

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



def normalize(package: str) -> str:
    return re.sub(r"[-_.]+", "-", package).lower()


def locked(name: str) -> dict[tuple[str, str], tuple[str, frozenset[str]]]:
    """(package, markers) -> (version, hashes) for a `uv pip compile --generate-hashes` file."""
    path = ROOT / name
    if not path.is_file():
        errors.append(f"{name}: missing")
        return {}
    entries = {}
    for line in path.read_text().replace("\\\n", " ").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        spec, *hashes = (part.strip() for part in line.split("--hash="))
        m = re.fullmatch(r"([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==([^\s;]+)\s*(?:;\s*(.*))?", spec)
        if not m:
            errors.append(f"{name}: '{spec}' is not an exact == pin")
            continue
        if not hashes:
            errors.append(f"{name}: {m[1]}=={m[2]} has no --hash, so pip cannot verify it")
        entries[(normalize(m[1]), m[3] or "")] = (m[2], frozenset(hashes))
    return entries


# The app installs requirements.txt; CI tests requirements-dev.lock. Both are compiled
# from requirements.in, so every package the app installs has to appear in CI's lock
# with the same version and hashes — otherwise CI passes on one set of packages while
# the app runs another.
app_pins = locked("requirements.txt")
ci_pins = locked("requirements-dev.lock")
for (package, markers), pin in sorted(app_pins.items()):
    if ci_pins.get((package, markers)) != pin:
        where = f"{package}=={pin[0]}" + (f" ; {markers}" if markers else "")
        errors.append(
            f"requirements.txt: {where} does not match requirements-dev.lock — regenerate both locks"
        )

# A pin bumped in requirements.in but never recompiled leaves the app on the old version.
app_versions = {package: version for (package, _), (version, _) in app_pins.items()}
if (ROOT / "requirements.in").is_file():
    for name, version in re.findall(
        r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==(\S+)", (ROOT / "requirements.in").read_text(), re.M
    ):
        if app_versions.get(normalize(name)) != version:
            errors.append(
                f"requirements.in pins {name}=={version} but requirements.txt has "
                f"{app_versions.get(normalize(name), 'no entry')} — regenerate both locks"
            )
else:
    errors.append("requirements.in: missing")

for problem in errors:
    print(f"error: {problem}", file=sys.stderr)
print(
    f"checked app.yaml, databricks.yml, target.yml.sample: "
    f"{len(declared)} bundle variables, {len(needs_value)} requiring a value from target.yml; "
    f"{len(app_pins)} app requirements hash-pinned and matching CI's lock"
)
sys.exit(1 if errors else 0)
