# Gate mechanics

What the nine stages do, how the failure presents, how to diagnose it, and which local tool to believe.

## Contents

1. The nine stages
2. What the Dependency Scanning gate actually does
3. The observed failure signature
4. Which analyser is the App Store running
5. Diagnosis, cheapest first
6. The two local tools
7. Escape hatches in detail
8. Fact, inference and unknown

## 1. The nine stages

**FACT**, from the PSIRENS 1.5.0 and 1.5.2 upload screens:

| # | Stage | Concern |
|---|-------|---------|
| 1 | Secret Detection | credentials committed to the archive |
| 2 | Dependencies | the declared dependency set |
| 3 | SAST Scan | static analysis of our own code |
| 4 | Dependency Scanning | known vulnerabilities in dependencies |
| 5 | Test | `pip install -r requirements.txt` then pytest |
| 6 | Code Quality | the SonarQube quality gate |
| 7 | Dockerfile Lint | Dockerfile hygiene |
| 8 | Container Build | the image builds |
| 9 | Container Scan | vulnerabilities in the built image |

**FACT, and useful.** The pipeline is not fail-fast. A Code Quality failure at stage 6 did not stop stages 7, 8 and 9 running on the failed PSIRENS 1.5.0 upload, and the summary read "8 of 9 stages passed". A failure at one stage still tells you what the later stages found.

**INFERENCE.** Stages 2 and 4 are distinct concerns, most likely resolve-and-inventory versus vulnerability matching. Neither stage's output has been read directly on a Bluestaq run.

**UNKNOWN.** What stage 2 does that stage 4 does not. The severity threshold that fails stages 4 and 9. Whether a waiver or allowlist mechanism exists on the platform.

## 2. What the Dependency Scanning gate actually does

● **FACT.** The analyser identifies itself in the job log as `dependency-scan-python`, version 6.6.1, and executes `/analyzer run` against the repository root.
● **FACT.** On success it writes two artefacts: `gl-sbom-*.cdx.json`, a CycloneDX Software Bill of Materials, and `gl-dependency-scanning-report.json`.
● **FACT.** The job log states "Dependency files in other directories will be skipped", and `DS_MAX_DEPTH` defaults to 2.
● **FACT**, from GitLab's public documentation for the current analyser. The default excluded paths are `**/spec`, `**/test`, `**/tests`, `**/tmp`, `**/node_modules`, `**/.bundle`, `**/vendor`, `**/.git`. Anything under those names is never seen, at any depth.
● **FACT**, same source. SBOMs are written next to the file that produced them, named `gl-sbom-<package-type>-<package-manager>.cdx.json`. The vulnerability report is written at the project root.
● **INFERENCE**, from the analyser's architecture. Dependency detection and SBOM generation happen *before* the vulnerability lookup. This is the diagnostic in section 5 and it splits the problem in half.

## 3. The observed failure signature

**FACT**, from the Legion pipeline job logs of 26 and 27 August 2026:

● Exactly two INFO lines, then `exit status 1` after roughly eight seconds.
● **No error text at all.** The platform swallows the analyser's stderr.
● `WARNING: ... no matching files` for both artefacts, so neither the SBOM nor the report was written.
● The App Store user interface renders the non-zero exit as "Vulnerable dependencies found. Update the flagged dependencies to versions without known vulnerabilities." No dependency is named, because none was flagged.

**FACT**, from GitLab's public documentation, and the important contrast: upstream, when the analyser finds no supported file, the job **completes successfully** and prints `No compatible file found in <directory>`. The Legion failure is therefore not that path. Something crashed, or a wrapper turned a warning into a failure.

**UNKNOWN.** The analyser's actual error message. Nobody has seen it. Until someone does, every explanation of this failure, including this one, is reasoning from the outside. If you are the person who finally sees that text, put it in this file.

## 4. Which analyser is the App Store running

This matters, because the two GitLab analyser lineages fail for different reasons and take different fixes.

● **Legacy Gemnasium lineage** (`gemnasium-python`): builds the project and runs pip to extract dependencies. Deprecated in GitLab 17.9, removed in 19.0. Its documented failure modes include hash mismatches, and requirements files generated on a different platform from the runner.
● **Current SBOM lineage** (`dependency-scanning:2`): does **not** build the project. It parses lockfiles and dependency graph exports, generates CycloneDX SBOMs, uploads them to the instance for matching, and offers a separate `.pre` stage resolution job that runs `pip-compile` for projects without a lockfile. Red Hat UBI based, FIPS 140-validated cryptographic module, deliberately minimal (no `grep` in the image).

**INFERENCE**, and it is the working assumption throughout this skill. The App Store runs a vendored build of the current SBOM lineage. Three reasons: the `/analyzer run` entrypoint, the `DS_*` variable family, and the "Dependency files in other directories will be skipped" log line, which reflects `DS_MAX_DEPTH` behaviour. The version string `dependency-scan-python 6.6.1` does not correspond to any public GitLab image tag, so the naming is Bluestaq's or a fork's.

**How to settle it, in one look at a pipeline.** If a job named something like `dependency-scanning:python-resolution` appears in a `.pre` stage, it is the current lineage with resolution enabled. If dependency extraction happens inside a single job that installs packages, it is the legacy lineage. Record the answer here the first time anyone sees a full pipeline listing.

**Consequence if it turns out to be the legacy lineage.** Two additional candidate causes come into play for Legion, both of which the current lineage does not have: a hash in `requirements.txt` that does not match the artefact the runner downloads, and a hash-locked file compiled for a different platform from the runner. Both are consistent with an eight-second failure. Neither applies if the current lineage is in use, because it never installs anything.

## 5. Diagnosis, cheapest first

1. **Look for the SBOM artefact.** No SBOM means a parse or classification crash, not a vulnerability. This is the cheapest and most decisive check available.
2. **Compare your package root against a known-passing package.** The single highest-value diagnostic there is, and it took four cycles to think of. Get a colleague's passing archive, list both roots, diff the filenames. That comparison produced the answer where source-reading had not.
3. **Get the real error text.** Use the "More Details" link on the failed gate, or have whoever owns the deployment repository re-run with `SECURE_LOG_LEVEL: debug`. On the current lineage, resolution jobs run with `allow_failure: true`, so a resolution failure is silent unless you go and read that job's log; set `CI_DEBUG_SERVICES: "true"` to capture the service container output.
4. **Check the resolver hazards.** In `requirements.txt`, `requirements.in`, `setup.py` or `pyproject.toml`, look for: Git or VCS URLs (`git+https://...`), which fail resolution outright for that file; `-e .`, `file:` or local path references, which are stripped with a warning so the package silently disappears from the output; `setup.py` with a dynamic `install_requires` read from a file at runtime; a `pyproject.toml` containing only `[build-system]` and no `[project]` table, which is skipped with a warning; a `Pipfile` without a `Pipfile.lock`, which is unsupported.
5. **Check placement.** Root and immediate subdirectories only. Nothing under `test`, `tests`, `spec`, `tmp`, `vendor`, or a hidden directory.
6. **Establish which set is implicated** once you have a real finding: runtime, test-only, or an operating system package from the base image. The fix differs completely in each case.
7. **Only then consider the escape hatches** in section 7.

## 6. The two local tools

They are easily confused. They answer different questions and have opposite evidential value.

### A locally built copy of the open-source analyser: diagnostic only

● **FACT.** GitLab's open-source analyser can be cloned, built with Go, and run against a package. Useful for reading the parse warnings the platform hides.
● **FACT.** It is not the platform's analyser, and its verdict is actively misleading. Calibrated against the three control samples it was wrong on two:

| Package | App Store gate | Locally built analyser |
|---|---|---|
| PSIRENS 1.5.3 | Passed | exit 1, no SBOM |
| Enlightenment 0.23.3 | Passed | exit 0, 27 components |
| Legion 0.4.3 | Failed | exit 0, 57 components |

The platform's analyser prints a line that exists nowhere in that codebase. Different tool. A local pass is not evidence you will clear the gate, and a local failure is not a reason to change your package. If you keep a wrapper script, put this calibration table in its header so nobody mistakes it for a pre-flight check.

### `pip-audit` against your own lock files: trustworthy, for a different question

`pip-audit` does not predict the gate either. It answers the question the gate refuses to answer: is the dependency set actually clean? It queries OSV, which aggregates the PyPA advisory database, GitHub advisories and NVD. Establish the answer yourself and keep the evidence. See `verification-loop.md`.

## 7. Escape hatches in detail

**FACT**, from GitLab's public documentation for the current analyser. These are CI/CD variables, so setting any of them means editing the pipeline file in the deployment repository.

| Variable | Default | What it does |
|---|---|---|
| `DS_PIPCOMPILE_LOCKFILE_FILE_NAME_PATTERN` | unset | Glob for which pip-compile lockfiles to scan, for example `requirements*.txt`. Filenames only, not paths |
| `DS_PIP_MANIFEST_FILE_NAME_PATTERN` | unset | Glob for which pip manifests feed resolution and manifest fallback |
| `DS_ENABLE_MANIFEST_FALLBACK` | `true` | Extract direct dependencies from a manifest when no lockfile is usable |
| `DS_MAX_DEPTH` | `2` | Directory levels searched. `-1` searches everything |
| `DS_EXCLUDED_PATHS` | see section 2 | Pre-filter applied before the scan, to detection and reachability alike |
| `DS_INCLUDE_DEV_DEPENDENCIES` | `true` | Not implemented for pip. Only Composer, Conda, Gradle, Maven, npm, pnpm, Pipenv, Poetry and uv honour it |
| `DS_ENABLE_VULNERABILITY_SCAN` | `true` | Turns the matching step off entirely |
| `DS_DISABLED_RESOLUTION_JOBS` | unset | Disable the `.pre` resolution job per ecosystem |
| `DS_STATIC_REACHABILITY_ENABLED` | `false` | Marks which components your code actually imports |
| `SECURE_LOG_LEVEL` | `info` | Set to `debug` to see what the platform hides |
| `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` | PyPI | Index used during resolution. See the warning below |

**Two cautions.**

First, `DS_EXCLUDED_PATHS` and `DS_ENABLE_VULNERABILITY_SCAN` can make a gate pass by scanning less, which is not the same as being secure. Reach for them only with the security owner's agreement, and record the decision.

Second, and this is a genuine security point rather than a convenience one: the resolution job executes ecosystem-native build tooling, which honours `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `setup.py` and lockfile install hooks. Anyone who can set those variables or modify the project's build files can cause arbitrary code to execute in a job holding `CI_JOB_TOKEN` and any masked variables in scope. Treat the resolution job as a sensitive execution context. Prefer protected, branch-scoped variables over a `variables:` block any developer can edit, and prefer committing a lockfile generated in a build you control over letting the platform resolve for you.

### Bring your own SBOM

The strongest option when resolution keeps failing. Generate a CycloneDX document yourself and hand it to the platform as a CI artefact report. Requirements: CycloneDX specification version 1.4, 1.5 or 1.6; compliance with the GitLab CycloneDX property taxonomy; uploaded as `artifacts:reports:cyclonedx` from a successful job. You then own the accuracy of that document, which is a real obligation, but it removes the resolver from the critical path entirely.

## 8. Fact, inference and unknown

**Established as fact**
● The failure presents with no error text, no SBOM, and a misleading label.
● `pyproject.toml` is the only root-file difference between the two passing packages and the failing one.
● Header form, hash-locking and multiple `.in`/`.txt` pairs do not decide the outcome: PSIRENS passes with a hand-written banner and no hashes, Enlightenment passes with a uv header and full hashes, Legion failed with a valid pip-compile header.
● A locally built open-source analyser disagrees with the platform on two of three known samples.
● The pipeline is not fail-fast.
● The platform test stage installs `requirements.txt`; the image installs the runtime file.

**Inference, acted on but not proven**
● That the App Store runs a vendored build of the current SBOM-lineage analyser.
● That adding `pyproject.toml` with a `[project]` table is what clears the gate. Two positive controls, one negative control, and a documented mechanism: a non-Poetry `pyproject.toml` is a resolution trigger that produces a lockfile via `pip-compile`.
● That stage 4 reads `requirements.txt` and never `requirements-runtime.txt`, so a divergence between them would ship a version no gate examined.

**Unknown**
● The analyser's real error message.
● The severity threshold at stages 4 and 9.
● Whether a waiver mechanism exists.
● What stage 2 does that stage 4 does not.
