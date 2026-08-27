# The package contract

Exactly what to ship, why each file is there, and the traps that look harmless.

## Contents

1. The three control samples
2. Which filenames the analyser can see
3. The resolution mechanism, and why `pyproject.toml` matters
4. The requirements split, and which file each stage reads
5. Templates
6. Traps
7. What is not the cause

## 1. The three control samples

Everything here is calibrated against the only three data points anyone has, all Python container applications for the same App Store:

| Application | Gate | `pyproject.toml` | `requirements.txt` header | Hashes | `.in` files |
|---|---|---|---|---|---|
| PSIRENS 1.5.3 | **Passed** | yes | hand-written banner | none | none |
| Enlightenment 0.23.3 | **Passed** | yes | uv header, line 1 | yes | yes, three pairs |
| Legion 0.4.3 | **Failed** | **no** | pip-compile header, line 2 | yes | yes, two pairs |

**FACT.** A file-level comparison of the three package roots found that `pyproject.toml` is the only file present in both passing packages and absent from the failing one. Every other difference was documentation.

## 2. Which filenames the analyser can see

**FACT**, from GitLab's public documentation for the current analyser, split by what the file is *for*. The distinction is the thing most people get wrong.

### Files that are scanned

| Package manager | File | Role |
|---|---|---|
| pip | `requirements.txt` | pip-compile lockfile. The primary path |
| pip | `requirements.txt` | Manifest, when used as fallback. Direct dependencies only |
| pip | `pipdeptree.json` | Dependency graph export from `pipdeptree --json` |
| pipenv | `Pipfile.lock`, `pipenv.graph.json` | Lockfile and graph export |
| poetry | `poetry.lock` | Lockfile |
| uv | `uv.lock` | Lockfile |

### Files that trigger resolution but are never themselves scanned

`requirements.txt`, `requirements.in`, `requirements.pip`, `requires.txt`, `setup.py`, `setup.cfg`, and `pyproject.toml` where it is not a Poetry project. These feed a `pip-compile` run that emits `pipcompile.lock.txt`, which is then scanned.

### Files the analyser does not know about at all

Anything else. In particular:

● `requirements-runtime.txt`, `requirements-dev.txt`, `requirements-runtime.in` are not registered names. Editing them cannot fix a scan failure and cannot cause one. Both passing applications ship extra `requirements-*.txt` files and pass regardless.
● `pylock.toml`, the PEP 751 standardised lock format, is **not** in the supported list. This is a trap in waiting: adopting it as your only lock file would make the gate blind to your dependencies while looking modern and correct. If you move to `pylock.toml`, keep exporting a `requirements.txt` alongside it.

### Placement rules

**FACT.** Root and immediate subdirectories only, `DS_MAX_DEPTH` default 2. Hidden directories ignored. Default exclusions remove anything under `spec`, `test`, `tests`, `tmp`, `node_modules`, `.bundle`, `vendor` and `.git`, at any depth. Moving a dependency file into a subdirectory removes it from the scan.

## 3. The resolution mechanism, and why `pyproject.toml` matters

**FACT**, from GitLab's documentation. For Python, when a supported manifest is present and no lockfile is committed, a resolution job runs in the `.pre` stage using a pip-tools image, executes `pip-compile`, and writes `pipcompile.lock.txt` for the scanning job to consume. The manifest files that trigger this are listed in section 2.

**INFERENCE, strongest available, and now with a mechanism rather than just a correlation.** The analyser's pip-tools package manager treats `requirements.txt` as a *lockfile* and expects a *requirements-type* file alongside it. `pyproject.toml` is the one both passing applications supply, and it is a documented resolution trigger. Its absence in Legion left the pip-tools path without the manifest half of the pair.

**FACT**, and the reason the template below is shaped as it is: a `pyproject.toml` that contains only build-system configuration and no `[project]` table is skipped, with a warning. An empty or build-only `pyproject.toml` buys you nothing.

## 4. The requirements split, and which file each stage reads

**FACT.** The platform test stage runs `pip install -r requirements.txt` then `pytest` against the package root, before it builds any image. The Docker image installs `requirements-runtime.txt`. `.dockerignore` excludes `tests/`. Those are two separate contracts and both must hold.

**FACT.** PSIRENS splits two ways; the delta between the files is exactly `pytest` and `pytest-cov`. Enlightenment splits three ways: runtime, test, and analyser tooling, each `.in` compiled to a hash-locked `.txt`, with the container installing the runtime file only under `pip install --require-hashes --no-deps`.

**INFERENCE, and the important one.** Because `requirements-runtime.txt` is not a registered filename, **stage 4 never reads the runtime set**. It reads `requirements.txt`, the test-inclusive superset. So stage 4 scans more than ships, and stage 9 scans the image. That is safe today only because the runtime pins are a version-identical subset. If the two ever diverged on a shared package, the image would ship a version no gate examined.

Two consequences:

● Keep the runtime file a strict subset, and assert it. The local loop's first leg does this; the platform does not.
● If you can get a pipeline change made, set `DS_PIPCOMPILE_LOCKFILE_FILE_NAME_PATTERN` to `requirements*.txt` so the runtime file is scanned too. This costs nothing and closes the gap properly.

Do not collapse the split into one file. You would widen the image's vulnerability surface at stage 9 to include the entire test toolchain. Note also that `DS_INCLUDE_DEV_DEPENDENCIES` is not implemented for pip, so you cannot ask the platform to ignore test tooling: the split is the only control you have over what stage 4 sees.

## 5. Templates

`assets/pyproject.toml.template`, reproduced here because it is short:

```toml
[project]
name = "your-app"
version = "0.1.0"
description = "One line, plain English."
requires-python = ">=3.12"
```

Deliberately no `[project.dependencies]` list. Neither passing application declares one. Dependencies belong in your pip-compile inputs and their locked outputs, which is what the test stage and the Dockerfile actually install. Declaring them twice lets the two drift, and drift is what the gate punishes.

Keep the version in step with your single source of truth automatically. If you have a `bump_version.sh`, have it rewrite that line; never edit it by hand.

One side effect: declaring `requires-python` may tighten the target version your linter infers, surfacing new lint findings. Fix them, do not suppress them.

Locking, from `scripts/lock.sh`:

```
uv pip compile --python-version 3.12 --generate-hashes requirements-runtime.in -o requirements-runtime.txt
uv pip compile --python-version 3.12 --generate-hashes requirements.in         -o requirements.txt
uv pip compile --python-version 3.12 --generate-hashes requirements-dev.in     -o requirements-dev.txt
```

`--python-version` is not optional. A lock file compiled for a different interpreter version resolves a different set, and on the legacy analyser lineage a platform mismatch is a documented failure mode.

## 6. Traps

● **`pip` only enforces hash checking when every requirement in the file has a hash**, or when `--require-hashes` is passed explicitly. A single unhashed line silently disables verification for that package. `uv pip compile --generate-hashes` avoids this by hashing everything; a hand-edited file does not.
● **Editable and local installs.** `-e .`, `file:` and local path references are stripped before resolution with a warning, so those packages simply do not appear in the output. You get a clean scan of an incomplete set.
● **Git and VCS dependencies.** `git+https://...` cannot be resolved. The resolution command fails for that manifest and continues with the others, so a partial result can look like a whole one.
● **A nested `Dockerfile`.** **FACT**, from an earlier failure on the same application: the container template is detected from a root-level `Dockerfile`, and a nested one breaks both template detection and the build context.
● **Shipping `.gitlab-ci.yml` inside the package.** Neither passing application does. That file lives in the deployment repository and a version upload does not update it. A copy is at best inert and at worst misleading to the next person.
● **Environment markers.** If a lock file contains two entries for the same package under different markers, only the first is parsed and reported. Constrain to one interpreter version and the problem does not arise.
● **`pylock.toml` as the only lock file.** See section 2.

## 7. What is not the cause

Ruled out by direct evidence. Do not spend a cycle on any of these.

● **An actual vulnerable package**, in the observed failure. No report artefact was produced, so nothing was flagged.
● **The lockfile header form.** Three header forms, two passes, one failure. If a document or an assistant tells you the fix is to regenerate `requirements.txt` so its header sits on the right line, that advice came from a different codebase. An earlier document, `DEPENDENCYSCANNING.md`, said exactly this; it was correct for what it was written about and does not transfer. Treat inherited diagnoses as hypotheses to test against your own package.
● **Missing hashes, or having hashes.** Both extremes pass. Keep hash-locking for supply-chain integrity; it is not what this gate fails on.
● **Several `requirements-*.in` and `.txt` pairs.** Enlightenment ships three and passes.
● **The Python version floor.** The passing pair declare `>=3.11` and `>=3.12,<3.13`.
