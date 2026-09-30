# DevOps

How lakebase-express is checked on every pull request, how its dependencies are
locked, and where releases stand. Setting up and deploying the app is in the
[README](../README.md).

## CI/CD

`.github/workflows/ci.yml` runs the merge checks on every pull request and every
push to `main`:

| Job | What it does |
| --- | --- |
| Lint | `ruff check` with the rule set in `ruff.toml`. Only syntax errors and undefined names (`E9`, `F63`, `F7`, `F82`) fail the build; every other finding is posted as a warning on the PR |
| Tests | `pytest tests/` on Python 3.10, 3.11, 3.12 and 3.13, then imports `backend.main:app` — the entrypoint the app's uvicorn command starts |
| Frontend | `npm run lockfile:check` (every package must be its own sha512-hashed tarball on registry.npmjs.org — no mirrors, git or other hosts; it runs before `npm ci` because that install's `postinstall` rewrites the lockfile), then `npm ci` and `npm run build` (`tsc -b` + `vite build`), and asserts `frontend/dist/index.html` exists |
| Deploy config | `.github/scripts/check_bundle_config.py`: `databricks.yml` and `target.yml.sample` parse, the app's `config.command` is declared in `databricks.yml` (`deploy.sh` renders `app.yaml` from it), every `${var.*}` in `databricks.yml` is declared, every variable without a default appears in `target.yml.sample`, and every package in `requirements.txt` is hash-pinned, matches `requirements-dev.lock` exactly, and agrees with the pins in `requirements.in` |

Nothing in CI needs a workspace or a secret — `tests/conftest.py` stubs the single
live lookup, so the suite runs with no Databricks credentials on the runner. Every
`GITHUB_TOKEN` permission is `contents: read`, every action is pinned to a full
commit SHA, and every `pip install` goes through the hash-pinned lock below.

`databricks bundle validate` is deliberately not part of this: it resolves the
current user against the workspace, so it needs credentials a pull request must not
have. `check_bundle_config.py` covers what is checkable offline instead.

### Releases and deployment

Both are still manual: build the SPA and run `./deploy.sh` (see
[Deploy as a Databricks App](../README.md#deploy-as-a-databricks-app)). Automating a release
means publishing artifacts from CI, which needs its own security approval, so the
tag-triggered release workflow and the environment-gated deploy workflow are held
back for a follow-up change rather than shipped here.

### Dependency locks

The app's direct dependencies live in `requirements.in` — edit pins there. Two
fully resolved, hash-pinned locks are generated from it:

- `requirements.txt` — what the app installs on Databricks Apps, and what
  `run_local.sh` installs locally.
- `requirements-dev.lock` — the same packages plus the test and lint tools from
  `requirements-dev.txt`. This is what CI installs.

`pip install -r requirements-dev.txt` also works and resolves fresh versions; the
lock is there when you want CI's exact set.

`requirements-dev.txt` adds only test and lint tooling on top of `requirements.in`
— nothing there ships in the app image. `httpx` backs FastAPI's `TestClient`, and
`pglast` parses the generated DDL in `tests/test_collations.py`.

```bash
uv pip compile requirements.in      --universal --python-version 3.10 --generate-hashes -o requirements.txt
uv pip compile requirements-dev.txt --universal --python-version 3.10 --generate-hashes -o requirements-dev.lock
```

Regenerate both whenever a pin in `requirements.in` or `requirements-dev.txt`
changes; the Deploy config job fails if the two locks drift apart or fall behind
`requirements.in`. `--universal` keeps one lock valid across the CI matrix (Linux
and macOS, Python 3.10-3.13) by carrying environment markers instead of resolving
for one interpreter.

Every entry in `requirements.txt` carries `--hash`, which puts pip in hash-checking
mode with no extra flags: the plain `pip install -r requirements.txt` that
Databricks Apps runs refuses any archive whose digest does not match, and so does
CI's `--require-hashes --no-deps` install. A swapped or re-uploaded release on PyPI
reaches neither the app nor the runner, and CI tests exactly the packages the app
runs.

One constraint on the runtime pins: keep them to versions Databricks' internal PyPI
mirror carries as well as PyPI, or the lock cannot be regenerated from a Databricks
laptop. The mirror also drops old releases — it stopped serving `fastapi` below
`0.115.12`, which is why the pin moved from `0.115.9` to `0.141.1` — so when
`uv pip compile` reports no matching version for a pin that used to lock, bump it.

### Lint scope

`ruff.toml` enables `E4`, `E7`, `E9`, `F` and `W` — the widest set that is already
clean here, so any finding a PR sees is one it introduced. Only syntax errors and
undefined names (`E9`, `F63`, `F7`, `F82`) fail the build; unused imports, `f`-strings
without placeholders and the rest show up as warnings on the PR, and it still
passes. Four modules carry documented `F821`/`F841` ignores: they pass a lambda that reads the
`except ... as exc` binding into `RunRegistry.update`, which calls it synchronously
inside the `except` block, so the code is correct and ruff's scope analysis is not.

Widening the gate is a separate piece of work: `ruff check --select E,F,W,I,UP,B`
reports roughly 700 findings, nearly all of them `E501` (line-too-long), plus a
few dozen unsorted-import, pyupgrade and bugbear hits. `ruff format` would rewrite
most of the codebase, so formatting is not part of the gate either.
