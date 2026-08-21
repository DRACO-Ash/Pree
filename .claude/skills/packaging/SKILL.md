---
name: packaging
description: Producing the deployable artifact. Use when building the package to deploy. Covers the static entrypoint-only zip with dated delivery copies and a SHA-256 integrity check, the container lean build context via .dockerignore, and version normalisation. The package must contain exactly what the deploy target expects and nothing else.
---

# Packaging

> Stack note: the commands shown here are the Node example. For Python, Java, Go, Rust, or .NET, run the equivalent canonical step from `toolchain-adapters`. The principle in this skill is what binds; the command is illustrative.

## Purpose and scope

How the deployable artifact is produced for each archetype. The static artifact is packaged as an entrypoint-only zip, with a dated delivery copy retained for rollback and a SHA-256 integrity check proving the zipped file matches source. The container archetype keeps a lean build context with `.dockerignore` so the image carries no cruft or secret. Scope is producing the package. It does not cover submitting it (`app-store-deployment`) or the deploy procedure (`release-and-deploy`).

## When to use

- Building the package immediately before a deploy.
- Producing a dated delivery copy for the record or for rollback.

## Prerequisites

- The verification loop green (`testing-standards`).
- `code-architecture` (what the entrypoint is); `release-and-deploy` (the container build).

## Procedure (static: entrypoint-only zip)

1. **Build a zip containing only the entrypoint.** No `.git`, no OS cruft, no tooling; just `${ENTRYPOINT_NAME}` at the root.
   ```
   ./scripts/build-package.sh <version> <date>
   unzip -l dist/<package>.zip      # expect exactly one file: ${ENTRYPOINT_NAME}
   ```
2. **Keep a dated delivery copy** named for the record, for example `Bluestaq_Limited_-_${PRODUCT}_-_V${MAJOR}_${MINOR}_-_<date>.html`. These copies are the rollback source (`release-and-deploy` resubmits a previous one).
3. **Prove integrity with SHA-256.** The zipped entrypoint's hash equals the source file's hash; record both.
   ```
   sha256sum ${SOURCE_PATH} ; unzip -p dist/<package>.zip ${ENTRYPOINT_NAME} | sha256sum
   ```

## Procedure (container: flat, lean build context)

1. **Put the `Dockerfile` and entrypoint at the package root, flat.** The App Store detects the container template from a root-level `Dockerfile` and generates a build that uses the root as context (`-f Dockerfile .`). Never wrap them in a subdirectory: a nested `Dockerfile` defeats template detection and breaks the build context, failing with `context must be a directory` (`app-store-deployment`).
2. **Exclude everything the image does not need** via `.dockerignore`: `node_modules`, `.git`, tests, `.env`, data, reports, CI files. The image is the build (`release-and-deploy`); the context must be small and secret-free.
3. **Ship a testable source tree in the upload zip.** For a quality-gated template (node, python, java) the platform runs your tests against the zip root before it builds the image, so the package must include `tests/`, `docs/`, and the test-runner config, not only the runtime source. The package allowlist and `.dockerignore` are separate contracts: one shapes the upload the platform tests, the other shapes the image it runs. A suite excluded from the zip fails the test stage in seconds and every later stage is skipped (`appstore-gate-compliance`).
4. **Normalise the version** so the artifact stamp, the manifest version, and the submission version field all agree.

## Decision rules

- **Anything beyond the entrypoint in the static zip?** Remove it; it trips the upload scan or flips template detection.
- **A secret or data file in the container context?** Exclude it in `.dockerignore`; a secret in any layer is a defect even in a "private" layer.
- **Version mismatch across stamp, manifest, and submission?** Normalise to one value before packaging.
- **Need to roll back later?** Keep the dated delivery copies; they are the rollback source.
- **Archetype changed (static to container, or the reverse)?** Re-derive the packaging allowlist from the new platform contract; never carry an exclusion list across an archetype boundary. The static package is a built artefact that bans tests; the container package is a testable source tree that ships them (`appstore-gate-compliance`).

## Standards (checkable assertions)

- Static: `unzip -l` lists exactly `${ENTRYPOINT_NAME}` and nothing else.
- Static: the zipped entrypoint's SHA-256 equals the source file's SHA-256.
- Static: a dated delivery copy is retained for the build.
- Container: the `Dockerfile` and entrypoint are at the package root (flat), not in a subdirectory, so `unzip -l` shows no wrapping folder.
- Container: `.dockerignore` excludes node_modules, .git, tests, .env, data, reports, and CI files.
- Container (quality-gated template): the upload zip is a testable source tree; `npm test -- --coverage` (or the stack equivalent) passes from a fresh unzip after `npm ci`, so the platform's test stage is green.
- The version is consistent across the artifact stamp, the manifest, and the submission field.

## Failure modes and remedies

- **Upload rejected by the scanner (static).** Cause: cruft in the zip. Fix: rebuild, re-check `unzip -l`.
- **Wrong template detected.** Cause: a stray container or manifest file in the static package. Fix: remove it; rebuild.
- **Image is large or carries a secret (container).** Cause: a missing `.dockerignore` entry. Fix: add the exclusion; rebuild.
- **`context must be a directory` or wrong template (container).** Cause: the `Dockerfile` is nested in a subdirectory of the package. Fix: flatten so the `Dockerfile` and entrypoint are at the root; rebuild and re-upload; never adapt the generated pipeline to the subdirectory.
- **Hashes differ (static).** Cause: the zip was built from a different file than source. Fix: rebuild from the verified source and re-hash.

## Verification

Static: `unzip -l` shows one file; the two SHA-256 values match; a dated copy exists. Container: build the image and confirm it does not contain `.git`, tests, or `.env` (inspect the context or the layers).

## Worked example

A static release: `./scripts/build-package.sh 2.8 24_Jun_2026` produces `dist/<package>.zip`; `unzip -l` shows only `index.html`; `sha256sum` on source and on the unzipped entry match (f7d5a92...); the dated copy `Bluestaq_Limited_-_ATLAS_-_V2_8_-_24_Jun_2026.html` is retained for rollback.

## Glossary

- **Entrypoint:** the filename the host serves, `${ENTRYPOINT_NAME}` (commonly `index.html`).
- **Dated delivery copy:** a named, retained build used as the rollback source.
- **Build context:** the files sent to the container build; kept lean via `.dockerignore`.
- **SHA-256:** the cryptographic hash used to prove the package matches source.
- Other terms: `glossary`.

## Provenance

Merged from the static bundle's build-package script and dated delivery and integrity practice, and the container bundle's `.dockerignore` lean-context rule, with version normalisation across the artifact stamp and submission field.
