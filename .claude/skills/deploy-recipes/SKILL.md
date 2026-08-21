---
name: deploy-recipes
description: Per-stack, copy-ready deployment recipes for the Bluestaq App Store container contract. Use when containerising a Java, Python, Go, Rust, or .NET service for deploy. Gives a minimal secure multi-stage Dockerfile, the test-with-coverage command and the exact coverage report path the SonarQube gate reads, the dependency vulnerability scanner, and the common pitfalls, for each stack. Makes seamless App Store deployment real beyond Node and static-html.
---

# Deploy recipes

## Purpose and scope

`toolchain-adapters` maps the canonical steps to each language; this skill is the concrete container recipe per stack for the App Store contract, so a non-Node service deploys as smoothly as a Node one. The runtime contract is fixed for every stack (`app-store-deployment`): read `process.env.PORT` defaulting to 8080, bind `0.0.0.0`, return HTTP 200 unauthenticated at `GET /` and the health paths, run as a non-root numeric user with no setuid binaries, and never set `ENV PORT` in the Dockerfile. The SonarQube quality gate reads coverage from a template-specific path, in a different format per language, so the test command must emit exactly that format. Scope is the per-stack recipe. The platform mechanics are `app-store-deployment`; the deploy procedure is `release-and-deploy`.

## When to use

- Containerising a Java, Python, Go, Rust, or .NET service for the App Store.
- A deploy fails on port, root user, health, or a missing coverage report and you need the correct shape.

## The three failures that bite every stack

Before the per-stack detail, the recurring contract breaks are: binding to `127.0.0.1`/`localhost` instead of `0.0.0.0`; hardcoding the port instead of reading `PORT` (default 8080); and a framework default that 301/302/307-redirects `GET /` (an HTTPS redirect or a trailing-slash normaliser), which breaks the unauthenticated-200 health contract. Add an explicit health route and remove in-cluster HTTPS redirects.

A fourth, at the packaging layer: the `Dockerfile` must sit at the **package root**, not in a subdirectory. The App Store detects the template from a root-level `Dockerfile` and builds from the root context (`-f Dockerfile .`); a nested `Dockerfile` fails the build with `context must be a directory`. Keep every recipe below flat at the package root (`packaging`, `app-store-deployment`).

A fifth, at the container-scan: the policy STOPS (not warns) on setuid/setgid files or directories (`suid_or_guid_set`) and on an unmitigated High/Critical CVE. The base image's bundled `npm` tree trips both at once (its directories are mode `02755`, and its deps carry CVEs such as `picomatch`), even when the app never uses npm at runtime. So harden every image below (`security-hardening`):

```dockerfile
# In the final image, as root before USER. Keep the fail-open upgrade in its own RUN, separate
# from the fail-closed strip, so a tolerated miss cannot swallow the mandatory step:
RUN apk -U upgrade --no-cache 2>/dev/null || (apt-get update && apt-get -y upgrade) || true
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx 2>/dev/null || true
# The suid/sgid sweep is the LAST mutation before the flatten, after user creation and all COPYs,
# because a later instruction can re-add the class (busybox adduser sets setgid on the home it makes):
RUN find / -xdev -perm /6000 \( -type f -o -type d \) -exec chmod a-s {} +
```

Prefer a multi-stage build so the package manager and toolchain never reach the final image; then the strip line is a cheap belt-and-braces. Sweep directories as well as files (`find / -xdev -perm /6000 -type f -exec chmod a-s {} +`, then `-type d`, since a file-only sweep misses setgid directories), and keep the fail-open `apk upgrade` in its own `RUN` so it cannot mask the fail-closed strip. Patch OS packages on build for the busybox/openssl-style Alpine CVEs. If the scan still shows path-less (`N/A`) `suid_or_guid_set` after a clean strip, it is reading **layer history**, an earlier base-image layer still carries bits a later `chmod` only masked; FLATTEN the runtime into one clean layer: do all hygiene in a `prep` stage, then ship `FROM scratch` with a single `COPY --from=prep / /` and re-declare all metadata plus an explicit `PATH` (`appstore-gate-compliance`, `security-hardening`).

## Materialised Node templates (copy-ready files)

The Node container scaffold ships as ACTUAL files under `templates/node/` beside this skill, not as prose you must remember to write: a hardened, flattened `Dockerfile`, a `run-tests.mjs` test entry that tolerates `npm test -- --coverage` and emits lcov, `sonar-project.properties`, a Sonar-equivalent `eslint.config.mjs`, `package-appstore.sh`, and `simulate-pipeline.sh`. Copy them, adjust the marked lines, and run `simulate-pipeline.sh` green before uploading. See `templates/node/README.md`. This exists because the field retrospective found that a scaffold which only described these defaults shipped a conventional Dockerfile and a bare `node --test`, and failed the platform despite a green local loop. The per-stack recipes below are the same shape for Java, Python, Go, Rust, and .NET, and they are now materialised the same way: each ships a `templates/<stack>/` set (a hardened Dockerfile, the stack's `sonar-project.properties` coverage scoping, a copy-ready README, and for Java an `application.properties`), reusing Node's stack-agnostic `package-appstore.sh` and `simulate-pipeline.sh`. Copy your stack's files, do not re-derive them; `/scaffold` does this for you.

## Recipes (replace every `<pinned-digest>` with the real image digest at build time)

### Java (Spring Boot)
```dockerfile
FROM maven:3.9-eclipse-temurin-21@sha256:<pinned-digest> AS build
WORKDIR /app
COPY pom.xml .
RUN mvn -B -e dependency:go-offline
COPY src ./src
RUN mvn -B -DskipTests clean package
FROM gcr.io/distroless/java21-debian13:nonroot@sha256:<pinned-digest>
WORKDIR /app
USER 65532:65532
COPY --from=build --chown=65532:65532 /app/target/*.jar app.jar
EXPOSE 8080
ENTRYPOINT ["java","-jar","app.jar"]
```
Make Spring honour the contract in `application.properties`: `server.port=${PORT:8080}`, `server.address=0.0.0.0`, expose `management.endpoint.health.probes.enabled=true`, and `permitAll()` on `/` and `/actuator/health` in Spring Security.
- Coverage: `mvn -B clean verify` with the JaCoCo `report` goal; path `target/site/jacoco/jacoco.xml` (JaCoCo XML); Sonar `sonar.coverage.jacoco.xmlReportPaths`.
- Scan: `mvn org.owasp:dependency-check-maven:check`.
- Pitfalls: JaCoCo emits binary `jacoco.exec` unless you add the `report` goal (Sonar then sees 0%); a hardcoded `server.port=8080` ignores the platform `PORT`; Spring Security without `permitAll()` on health makes it 401.

### Python (FastAPI or Flask)
```dockerfile
FROM python:3.12-slim@sha256:<pinned-digest> AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --require-hashes -r requirements.txt
FROM python:3.12-slim@sha256:<pinned-digest>
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true && \
    useradd -u 10001 -r -s /usr/sbin/nologin appuser
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY --chown=10001:10001 . .
USER 10001:10001
EXPOSE 8080
CMD ["sh","-c","exec gunicorn app:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-8080}"]
```
- Coverage: `coverage run -m pytest && coverage xml` (or `pytest --cov=app --cov-report=xml`); path `coverage.xml` (Cobertura); Sonar `sonar.python.coverage.reportPaths`.
- Scan: `pip-audit -r requirements.txt`.
- Pitfalls: uvicorn/gunicorn default to `127.0.0.1`, you must pass `-b 0.0.0.0:${PORT}`; only the `xml` report produces the Cobertura file; omitting `exec` means SIGTERM never reaches gunicorn and shutdown hangs.

### Go
```dockerfile
FROM golang:1.23@sha256:<pinned-digest> AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /app ./cmd/server
FROM gcr.io/distroless/static-debian13:nonroot@sha256:<pinned-digest>
COPY --from=build /app /app
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/app"]
```
In code: `port := os.Getenv("PORT"); if port=="" { port="8080" }; http.ListenAndServe("0.0.0.0:"+port, mux)`, with `/` and `/health` returning 200.
- Coverage: `go test ./... -coverprofile=coverage.out -covermode=atomic`; Sonar reads it natively via `sonar.go.coverage.reportPaths=coverage.out` (convert with `gcov2lcov` only if your gate insists on lcov).
- Scan: `govulncheck ./...`.
- Pitfalls: never bind `localhost`; `http.FileServer` and `gorilla/mux` `StrictSlash(true)` 301-redirect `/`; always default an empty `PORT` to 8080.

### Rust
```dockerfile
FROM rust:1.83-slim@sha256:<pinned-digest> AS build
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
RUN mkdir src && echo "fn main(){}" > src/main.rs && cargo build --release && rm -rf src
COPY . .
RUN cargo build --release --locked
FROM gcr.io/distroless/cc-debian13:nonroot@sha256:<pinned-digest>
COPY --from=build /app/target/release/server /usr/local/bin/server
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/server"]
```
In code (axum/actix): read `PORT` (default 8080), bind `0.0.0.0`, no auth middleware on `/` and `/health`.
- Coverage: `cargo llvm-cov --lcov --output-path coverage/lcov.info` (needs the `llvm-tools-preview` component); lcov is the portable target.
- Scan: `cargo audit`.
- Pitfalls: a default (glibc) build on `distroless/static` fails to start, use `cc` base or build musl static; bind `0.0.0.0` not `127.0.0.1`; without `llvm-tools-preview`, coverage generation silently yields an empty `lcov.info`.

### .NET (ASP.NET Core)
```dockerfile
FROM mcr.microsoft.com/dotnet/sdk:8.0@sha256:<pinned-digest> AS build
WORKDIR /src
COPY *.csproj ./
RUN dotnet restore
COPY . .
RUN dotnet publish -c Release -o /app --no-restore
FROM mcr.microsoft.com/dotnet/aspnet:8.0-jammy-chiseled@sha256:<pinned-digest>
WORKDIR /app
COPY --from=build /app .
EXPOSE 8080
ENTRYPOINT ["dotnet","YourApp.dll"]
```
ASP.NET reads `ASPNETCORE_HTTP_PORTS`/`ASPNETCORE_URLS`, not `PORT`. The `aspnet:8.0` chiselled base already defaults to 8080 and ships the numeric non-root `app` user, so the contract holds without `ENV PORT`. To honour a platform-injected `PORT` too: `builder.WebHost.UseUrls($"http://0.0.0.0:{Environment.GetEnvironmentVariable("PORT") ?? "8080"}")`. Map `/` and `/health` to 200 unauthenticated.
- Coverage: `dotnet test --collect:"XPlat Code Coverage" -p:CoverletOutputFormat=opencover`; Sonar reads OpenCover via `sonar.cs.opencover.reportsPaths` (Sonar does NOT consume Cobertura for C#), or dotnet-coverage XML via `sonar.cs.vscoveragexml.reportsPaths`.
- Scan: `dotnet list package --vulnerable --include-transitive`.
- Pitfalls: the default Coverlet Cobertura output is ignored by Sonar's C# analyser, use OpenCover; `app.UseHttpsRedirection()` 307-redirects `/`, remove it for the in-cluster HTTP listener; do not override the chiselled `ASPNETCORE_HTTP_PORTS=8080` with `ENV PORT`.

## Decision rules

- **Which base?** Prefer distroless or chiselled (no shell, no setuid, numeric non-root). Use `cc`/glibc or musl-static deliberately for Rust and CGO Go.
- **Coverage gate red?** Confirm you emit the stack's format at the path Sonar expects (jacoco.xml, coverage.xml/Cobertura, lcov, Go profile, OpenCover for C#); convert if needed.
- **`GET /` not 200?** Remove the in-cluster HTTPS redirect and add an explicit unauthenticated health route.
- **Image runs as root?** Add a numeric `USER` and strip setuid bits; the platform expects non-root.

## Standards (checkable assertions)

- The image runs as a non-root numeric user, binds `0.0.0.0`, reads `PORT` (default 8080), returns 200 at `/` and health, and sets no `ENV PORT`.
- The base image digest is pinned.
- The test command emits coverage in the format and at the path the SonarQube gate reads for the stack, at 80% or more.
- The dependency vulnerability scan runs for the stack and blocks on High or Critical.

## Failure modes and remedies

- **Pod Degraded with no log.** Cause: bound to loopback or wrong port. Fix: bind `0.0.0.0:${PORT:-8080}`.
- **Root probe returns 3xx.** Cause: HTTPS redirect or trailing-slash normaliser. Fix: explicit 200 health route; drop in-cluster redirect.
- **Coverage shows 0%.** Cause: wrong format or path. Fix: emit the stack's expected report (see each recipe).
- **Image flagged for setuid or root.** Fix: distroless/chiselled base, numeric `USER`, strip setuid bits.

## Verification

`docker build` succeeds; `docker run --rm <img> id` shows a non-zero UID; `curl -i localhost:8080/` and the health path return 200; the test command writes coverage at the gate's expected path at 80% or more; the vulnerability scan is clean of unaddressed High/Critical. Then `deploy-gate` validates the submission (`app-store-deployment`).

## Glossary

- **Distroless / chiselled:** a minimal base with no shell, package manager, or setuid binaries; ships a numeric non-root user.
- **OpenCover / JaCoCo / Cobertura / lcov:** per-language coverage report formats the SonarQube gate consumes.
- **`APP_UID`:** the .NET 8 base image's numeric non-root user.
- Other terms: `glossary`, `toolchain-adapters`, `app-store-deployment`.

## Provenance

Authored from researched, source-verified per-stack container practice (distroless and chiselled bases, the SonarQube coverage-path matrix, the official per-language vulnerability scanners) against the App Store runtime contract in `app-store-deployment`, complementing the command matrix in `toolchain-adapters`.
