---
name: appstore-python-gate
description: Bluestaq App Store supply-chain gate readiness for Python container applications. Use this skill whenever a Bluestaq App Store upload fails or is about to be attempted, and whenever Ash mentions Dependency Scanning, "Vulnerable dependencies found", Secret Detection, SAST Scan, Container Scan, Dockerfile Lint, an SBOM, pip-audit, a CVE in a dependency, "the gate failed", "no SBOM", "8 of 9 stages passed", requirements.txt, pyproject.toml, uv lock, pip-compile, hash pinning, base image digests, or preparing a package for upload. Force-triggers on PSIRENS, Enlightenment, Legion, package-appstore.sh, simulate-pipeline.sh, or any question of the form "why did the gate fail" or "will this pass". Also use when hardening a Python application against supply-chain risk generally, when asked what the App Store does not check, or when SBOM or vulnerability-handling evidence is needed for CRA, NIST SSDF, IASME or customer assurance. The gate reports the wrong cause by design; never start by upgrading packages.
---

# App Store Python gate

Getting a Python container application through the Bluestaq App Store supply-chain gates, and covering the risks those gates do not cover.

## The one thing to internalise first

A `Dependency Scanning` failure almost never means a vulnerable dependency. It usually means the analyser crashed while working out what your dependency files are, and the platform relabelled a non-zero exit code as "Vulnerable dependencies found. Update the flagged dependencies to versions without known vulnerabilities."

Do not upgrade a single package until the SBOM check in step 1 below has been done. Four upload cycles were spent on that mistake once already.

## Confidence markers

Everything in this skill is marked, and the marking is load-bearing. Preserve it in any output.

● **FACT**: observed directly, in a Bluestaq pipeline run, in a repository, in vendor documentation, or by running the code.
● **INFERENCE**: reasoned from those observations, consistent with everything seen, not proven against the App Store's own binary.
● **UNKNOWN**: genuinely not established. Do not let anyone promote these quietly.

Where an assertion comes from GitLab's public documentation rather than a Bluestaq run, say so. The App Store analyser is a vendored build and public documentation is a strong guide, not a guarantee.

## Step 1: triage before touching anything

Ask these four questions in order. Stop at the first that gives an answer.

1. **Did the failed job produce an SBOM artefact?** SBOM generation happens before the vulnerability lookup. No `gl-sbom-*.cdx.json` means a parse or classification crash, not a finding. If an SBOM exists and the job still failed, and `gl-dependency-scanning-report.json` names a package, you have a real advisory: go to `references/verification-loop.md`, "When an advisory fires".
2. **Does the package root carry a `pyproject.toml` with a `[project]` table?** Absence is the single highest-value difference between the packages that pass and the one that failed. See step 2.
3. **Does `requirements.txt` contain anything the resolver cannot handle?** Git or VCS URLs, `-e .`, `file:` paths, or local path references. Each of these is a documented resolution failure or silent strip.
4. **Is every dependency file at the repository root?** Only the root and its immediate subdirectories are scanned, and `**/test`, `**/tests`, `**/spec`, `**/tmp`, `**/vendor` and `**/.git` are excluded by default.

If none of those four resolves it, read `references/gate-mechanics.md` in full and work the diagnosis order there.

## Step 2: the package contract

Ship exactly this shape. It is the intersection of the two applications known to clear the gate, plus the mechanism visible in the analyser's documented behaviour.

```
package-root/
├── pyproject.toml            REQUIRED. Must contain a [project] table.
├── requirements.txt          The pip-compile or uv lockfile. Scanned directly.
├── requirements.in           The input. Not scanned; drives resolution.
├── requirements-runtime.in   Runtime-only input.
├── requirements-runtime.txt  Installed by the image. NOT scanned by default.
├── requirements-dev.in       Analyser tooling input.
├── requirements-dev.txt      Installed by the platform test stage.
├── Dockerfile                Root level. A nested one breaks template detection.
├── src/                      Application code.
├── tests/                    Must be inside the package; excluded from the image.
├── .dockerignore             Excludes tests/, .venv/, .git/
└── scripts/verify.sh         The local loop.
```

Copy `assets/pyproject.toml.template`, change three values, add nothing else. Deliberately no `[project.dependencies]` list: neither passing application declares one, and declaring dependencies twice lets the two drift.

Do **not** ship a `.gitlab-ci.yml` inside the package. That file lives in the deployment repository and a version upload does not update it.

Full rationale, the filename table the analyser actually reads, and the resolution mechanism are in `references/package-contract.md`.

## Step 3: prove the dependency set is clean, separately

The gate will not tell you. Establish it yourself and keep the evidence, so that when the gate fails you can say plainly that you have a tooling failure rather than a security finding.

Run `scripts/preflight.py` against the package root. It is standard library only, has a self-test, and prints findings with confidence markers. It checks the contract in step 2, the resolver hazards in step 1, and the local-loop guards.

Then run the six-leg verification loop. The design and the four rules it encodes (a failure to check is never a pass; an offline runner says so in the banner; every tool runs through one resolved interpreter; a gating command is never piped) are in `references/verification-loop.md`. Adopt them rather than reinventing them; each one exists because its absence produced a false verdict.

## Step 4: cover what the App Store does not

The nine stages are a narrow instrument. They say nothing about base image drift, malicious-but-not-yet-disclosed packages, dependency confusion, provenance, or your obligations under the EU Cyber Resilience Act, whose vulnerability reporting duty binds from 11 September 2026.

`references/beyond-the-gate.md` is the gap register: what each stage does not see, what to put in place instead, and how the evidence maps to NIST SSDF, the 2026 CISA SBOM minimum elements, the CRA and the UK Software Security Code of Practice. Read it before any customer assurance or certification submission, and before telling anyone that a green pipeline means the application is secure.

`references/optional-testing.md` covers the assurance layers worth adding above the gate: static reachability, mutation testing, property-based testing, coverage-guided fuzzing, VEX, and how to decide which are worth their cost on a given application.

## Escape hatches, and the price of each

These require editing the pipeline file in the deployment repository, which an application team usually cannot do. Reach for them only with the security owner's agreement, and record why.

| Lever | Use | Price |
|---|---|---|
| `DS_PIPCOMPILE_LOCKFILE_FILE_NAME_PATTERN` | Get `requirements-runtime.txt` scanned as well | None. This closes a real gap, see `beyond-the-gate.md` |
| `DS_ENABLE_MANIFEST_FALLBACK` | Proceed from a plain manifest when no lockfile is usable | Direct dependencies only, no transitives. Reduced accuracy, not reduced risk |
| `SECURE_LOG_LEVEL: debug` | Read the error text the platform swallows | None. Do this first, not last |
| `DS_EXCLUDED_PATHS` | Exclude noisy paths | Makes a gate pass by scanning less. Not the same as being secure |
| `DS_ENABLE_VULNERABILITY_SCAN: false` | Turn the check off | As above, and worse. Requires written sign-off |
| Bring your own SBOM | Upload a CycloneDX 1.4-1.6 document you generated | You own its accuracy. The strongest option when resolution keeps failing |

## Reference files

Read the one that matches the question. Do not read all four by default.

● `references/gate-mechanics.md`: what each of the nine stages does, the observed failure signature, the diagnosis order, the two local tools and why only one of them counts, and the analyser lineage question.
● `references/package-contract.md`: the filename table, the resolution mechanism, templates, and the traps (pylock.toml, editable installs, platform-specific hashes, nested files).
● `references/verification-loop.md`: the local loop, the four rules, lock file discipline, and what the loop does not cover.
● `references/beyond-the-gate.md`: the gap register, standing risks in the current posture, the global regulatory map, and the 2026 threat picture.
● `references/optional-testing.md`: assurance layers above the gate, with a cost and value judgement on each.

## Assets and scripts

● `scripts/preflight.py`: standard library only, self-testing gate readiness checker. Run it before every upload. `--self-test` verifies the checker itself.
● `scripts/lock.sh`: canonical hash-locked regeneration of all three requirements files, for one pinned interpreter version.
● `assets/pyproject.toml.template`: the minimal file that satisfies the resolver, with the Ruff security rule selection included.
● `assets/Dockerfile.template`: digest-pinned base, non-root, fail-closed patch step, runtime-only install under `--require-hashes`.
● `assets/dockerignore.template`: the two separate contracts for `tests/`, stated in the file.

## Standing rules

● Never edit a `.txt` lock file by hand. The hashes are what make `pip install --require-hashes --no-deps` meaningful.
● Bump, do not remove. Every entry in these dependency sets is load-bearing; removing a transitive pin to silence a scanner will break the install.
● No new runtime dependency without a recorded reason. A small, boring, framework-shaped dependency set is the cheapest supply-chain control available.
● Record what a job log actually said, converting an UNKNOWN into a FACT. Nobody has yet seen the analyser's real error text. That one paragraph would be worth more than the rest of this skill.
