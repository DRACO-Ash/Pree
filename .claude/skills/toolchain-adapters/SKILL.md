---
name: toolchain-adapters
description: The stack-agnostic adapter layer that makes every other skill ubiquitous. Use whenever a skill says "run your stack's equivalent" or when the project is not Node. Maps the canonical engineering steps (pin the runtime, install reproducibly, lint and type-check, test with coverage, scan dependencies, build and package, containerise) to concrete commands for Node, Python, Java, Go, Rust, and .NET, and explains how the archetype and App Store template are decided independently of language.
---

# Toolchain adapters

## Purpose and scope

The Foundations standard is a set of universal principles (fail closed, validate at the boundary, one accent, no secret in the repo, a green loop before a gate, a human-confirmed deploy). The principles do not change with language; the commands do. This skill is the adapter: it states each canonical engineering step once, then gives the concrete command for each supported stack, so the other skills can stay language-neutral and a Python, Java, Go, Rust, or .NET project follows the same standard as a Node one. Scope is the command mapping and the archetype/template decision. The principles themselves live in the owning skills (`testing-standards`, `dependencies`, `security-hardening`, `packaging`, `release-and-deploy`, `app-store-deployment`).

## When to use

- The project is not a Node app and a skill's snippet shows an `npm` command.
- You are setting up the environment, the verification loop, or the package for any stack.
- You need to know which App Store template a project detects as, and why that is separate from its language.

## The model: archetype and language are orthogonal

Two independent choices decide how a project is built and shipped.

1. **Archetype** is about runtime shape, not language.
   - **Static**: no server process at runtime. A single self-contained artifact (an offline HTML page, a compiled WebAssembly bundle, a built single-page app) served as files. No egress.
   - **Server**: a process runs at runtime (an HTTP API, a worker, a database client, an LLM call). Shipped as a container.
   A Rust program compiled to a static page is **static**; a Python service is **server**; a Go binary behind HTTP is **server**. Decide the archetype first (`getting-started` Step 0), then pick the language.

2. **App Store template** is auto-detected from the package contents, independent of how you wrote the code (`app-store-deployment`): `static-html` (only the entrypoint, no server marker, no container file), `node-react` (`package.json`), `java-spring` (`pom.xml`), `python` (`requirements.txt`), or `docker-only` (a `Dockerfile` and no other marker). A container present means a container template.

The standard binds at both levels: the archetype decides the verification loop shape, the template decides the deploy contract. The language only decides the commands in this table.

## Choosing a language (quick guide)

The standard is identical whichever you pick; this is only about fit. There is no wrong answer.

| Stack | Good at | Less good at | Typically used for |
|---|---|---|---|
| Static (HTML, CSS, JS) | One self-contained, offline file; cheapest to host, hardest to break | Storing data, server logic, holding secrets | Content sites, docs, single-page tools, guides |
| Node (JS, TS) | Web APIs and full-stack web, fast iteration, huge ecosystem, real-time | CPU-bound number-crunching | Web services, single-page app back ends, tooling |
| Python | Data, scripting, machine learning and AI, readable and quick to write | Raw runtime speed, mobile | APIs (FastAPI, Flask, Django), data pipelines, automation, ML |
| Java | Large, long-lived systems, strong typing, mature tooling, JVM performance | Quick scripts, lean start-up time | Enterprise back ends (Spring Boot), Android, big systems |
| Go | Small fast single binaries, easy concurrency, simple deploys | Abstraction-heavy designs, graphical apps | Microservices, command-line tools, networking, infrastructure |
| Rust | Top performance with memory safety, high reliability, no garbage collector | Fast prototyping; steeper curve, slower builds | Systems software, performance-critical services, WebAssembly |
| .NET (C#) | Enterprise web and desktop, strong typing, excellent tooling, cross-platform | Lightweight one-off scripting | ASP.NET web APIs, desktop, games (Unity), enterprise |

Decide the archetype first (does a process run at runtime?), then the language from the row above. The App Store template then follows from the package markers, not the language.

## Procedure: the canonical steps and their per-stack commands

Each row is a canonical step. Run the cell for your stack. Where a skill elsewhere shows the Node cell, this table is the authority for the rest.

### 1. Pin the runtime (`environment-setup`)

| Stack | Pin file | Verify |
|---|---|---|
| Node | `.nvmrc` or `engines` in `package.json` | `node --version` |
| Python | `.python-version` or `requires-python` in `pyproject.toml` | `python --version` |
| Java | `.sdkmanrc` or the toolchain block in the build file | `java -version` |
| Go | the `go` directive in `go.mod` | `go version` |
| Rust | `rust-toolchain.toml` | `rustc --version` |
| .NET | `global.json` | `dotnet --version` |

The rule (universal): the runtime major is pinned in a tracked file and verified before any work. Never float it.

### 2. Install dependencies reproducibly, from a committed lockfile (`dependencies`)

| Stack | Reproducible install | Lockfile |
|---|---|---|
| Node | `npm ci` (or `pnpm i --frozen-lockfile`, `yarn --immutable`) | `package-lock.json` |
| Python | `pip install -r requirements.txt --require-hashes`, or `uv sync --frozen`, or `poetry install --no-update` | `requirements.txt` (hash-pinned), `uv.lock`, or `poetry.lock` |
| Java | `mvn -o verify` (offline against the local repo), or Gradle `--offline` | `pom.xml` exact versions, or `gradle.lockfile` |
| Go | `go mod download` then build (verifies `go.sum`) | `go.sum` |
| Rust | `cargo build --locked` | `Cargo.lock` |
| .NET | `dotnet restore --locked-mode` | `packages.lock.json` |

The rule (universal): exact pins, a committed lockfile, a clean install that fails if the manifest and lockfile disagree, and the lockfile unchanged afterwards.

### 3. Lint, format, and type-check (`code-architecture`, the post-edit gate)

| Stack | Format | Lint and static analysis | Types |
|---|---|---|---|
| Node | `prettier --check` | `eslint` | `tsc --noEmit` (TypeScript) |
| Python | `ruff format --check` or `black --check` | `ruff check` | `mypy` or `pyright` |
| Java | `spotless:check` | `checkstyle`, `spotbugs` | the compiler |
| Go | `gofmt -l` (empty output) | `go vet ./...`, `staticcheck ./...` | the compiler |
| Rust | `cargo fmt --check` | `cargo clippy -- -D warnings` | the compiler |
| .NET | `dotnet format --verify-no-changes` | the built-in analysers | the compiler |

### 4. Test with coverage, at least 80% (`testing-standards`)

| Stack | Test with coverage | Coverage report path |
|---|---|---|
| Node | `c8`/`nyc` around `node:test`, lcov reporter | `coverage/lcov.info` |
| Python | `pytest --cov --cov-report=xml` | `coverage.xml` |
| Java | `mvn test` with JaCoCo | `target/site/jacoco/jacoco.xml` |
| Go | `go test ./... -coverprofile=coverage.out` (convert to lcov for the gate) | `coverage.out` -> `lcov.info` |
| Rust | `cargo llvm-cov --lcov --output-path lcov.info` | `lcov.info` |
| .NET | `dotnet test --collect:"XPlat Code Coverage"` | `coverage.cobertura.xml` |

The static archetype's loop is different and language-light: validate the entrypoint, render-check it headless on desktop and mobile, and run the static security greps (`testing-standards`). That loop is the same whatever produced the static file.

### 5. Scan dependencies for known vulnerabilities (`security-hardening`, `dependencies`)

| Stack | Vulnerability scan |
|---|---|
| Node | `npm audit` and `npm audit --omit=dev` |
| Python | `pip-audit` |
| Java | the OWASP dependency-check plugin |
| Go | `govulncheck ./...` |
| Rust | `cargo audit` |
| .NET | `dotnet list package --vulnerable --include-transitive` |

The rule (universal): any unaddressed High or Critical is a gate failure; upgrade and re-pin, or document the suppression with justification.

### 6. Build and package (`packaging`, `release-and-deploy`)

| Stack | Static build (if applicable) | Server build |
|---|---|---|
| Node | the entrypoint-only zip (`build-package.sh`) | `docker build` |
| Python | n/a (usually server) | `docker build`, or a wheel for a library |
| Java | n/a | `mvn package` then `docker build` of the jar |
| Go | a single binary served as files only if it emits a static site | `go build` then `docker build` (distroless or scratch) |
| Rust | a `wasm`/static-site build served as files | `cargo build --release` then `docker build` |
| .NET | n/a | `dotnet publish -c Release` then `docker build` |

The container contract is identical for every server stack (`app-store-deployment`): read `process.env.PORT` defaulting to 8080, bind `0.0.0.0`, return 200 at `GET /` and the health paths, run as a non-root numeric user, no `ENV PORT=`.

## Decision rules

- **A skill shows an `npm` command and my project is not Node?** Map it through the matching row here. The principle in that skill is what binds; the command is an example.
- **Which archetype?** Decide by runtime shape (does a process run?), not by language (`getting-started` Step 0).
- **Which template?** It is auto-detected from package markers; do not try to force it. Remove a stray marker (a `Dockerfile` in a static app) rather than fighting detection.
- **My stack is not in the table?** Add a row in the same shape (pin, install, lint, test+coverage, scan, build) and record the chosen commands in `CLAUDE.md`. The six canonical steps are the contract; the table is extensible.
- **Coverage report in a format the gate does not list?** Convert it to one the SonarQube gate reads (lcov, jacoco.xml, cobertura, coverage.xml) at the path the template expects.

## Standards (checkable assertions)

- The runtime major is pinned in a tracked file and verified before work, for the project's stack.
- Dependencies install reproducibly from a committed lockfile that is unchanged after a clean install.
- Lint, type-check, and the vulnerability scan run for the stack, and a High or Critical blocks.
- The test command produces a coverage report at the path the App Store template expects, at 80% or more (server archetype).
- The container contract holds for any server stack: port 8080 default, `0.0.0.0`, health 200, non-root.
- `CLAUDE.md` records the project's concrete commands for each canonical step.

## Failure modes and remedies

- **A non-Node project has no lockfile.** Fix: adopt the stack's lockfile (`poetry.lock`, `go.sum`, `Cargo.lock`, `packages.lock.json`, `gradle.lockfile`) and install in locked mode.
- **Coverage gate red because the report is missing.** Fix: write coverage to the template's expected path (`coverage/lcov.info`, `target/site/jacoco/jacoco.xml`, `coverage.xml`); convert if needed.
- **Wrong template detected.** Fix: a stray container file or manifest is present; remove it and rebuild (`packaging`).
- **The vulnerability scanner is not wired.** Fix: add the stack's scanner to the loop and to CI so a CVE fails the build, not a human's memory.

## Verification

For the project's stack: the pin verifies, the locked install leaves the lockfile unchanged, lint and type-check are clean, the test command writes coverage at the expected path at 80% or more, the vulnerability scan is clean of unaddressed High/Critical, and (server) the built container answers 200 on the health paths as a non-root user on port 8080. When every canonical step has a green command for the stack, the project is on the standard regardless of language.

## Worked example

A Python FastAPI service. Archetype: server (a process runs). Template: `python` (`requirements.txt` present). Steps: pin Python in `.python-version` (`python --version` confirms); `uv sync --frozen` (the `uv.lock` is unchanged); `ruff check` and `mypy` clean; `pytest --cov --cov-report=xml` writes `coverage.xml` at 86%; `pip-audit` clean; `docker build` of an image that reads `PORT` (default 8080), binds `0.0.0.0`, returns 200 at `/healthz`, and runs `USER 1000:1000`. The same `engineering-reviewer`, `security-reviewer`, and `deploy-gate` then apply unchanged, because they check the canonical properties, not the language.

## Glossary

- **Archetype:** static (no runtime process) or server (a runtime process), independent of language.
- **Template:** the App Store build type auto-detected from package markers (one of five).
- **Adapter:** the per-stack command for a canonical step.
- **Locked install:** a reproducible install that fails if the manifest and lockfile disagree.
- Other terms: `glossary`.

## Provenance

Authored to generalise the Foundations baseline beyond its original two Node archetypes, drawing the canonical step set from `environment-setup`, `dependencies`, `testing-standards`, `security-hardening`, `packaging`, and `release-and-deploy`, and the template matrix and coverage paths from `app-store-deployment`. The per-stack commands follow each ecosystem's standard reproducible-build and coverage tooling.
