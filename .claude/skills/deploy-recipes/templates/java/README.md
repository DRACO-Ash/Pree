# Java (Spring Boot) container scaffold templates (App Store, quality-gated)

Copy-ready files for a Spring Boot server app targeting the Bluestaq App Store container
template. They materialise the `deploy-recipes` Java recipe as real files you copy, so the App
Store contract holds from the first commit instead of being prose you must remember.

## The files

- **`Dockerfile`** - hardened, multi-stage image: Maven builds the jar, then it ships on a
  distroless java nonroot base (no shell, no package manager, numeric non-root user). Copy to
  the repository root and replace every `<pinned-digest>`.
- **`application.properties`** - the runtime-contract settings: `server.port=${PORT:8080}`,
  `server.address=0.0.0.0`, health probes enabled, and the Spring Security `permitAll()` note.
  Merge into `src/main/resources`.
- **`sonar-project.properties`** - Code Quality gate scoping. Coverage is JaCoCo XML at
  `target/site/jacoco/jacoco.xml`, read via `sonar.coverage.jacoco.xmlReportPaths`. Commit at
  the repository root.

## How to use

1. Copy the `Dockerfile` and `sonar-project.properties` to the repository root and merge
   `application.properties`. Replace every `<pinned-digest>` and `CHANGE_ME_project_key`.
2. The runtime contract is carried by `application.properties`: read `PORT` (default 8080),
   bind `0.0.0.0`, and `permitAll()` on `/` and `/actuator/health` so health returns 200, not
   401. Never set `ENV PORT` in the Dockerfile.
3. Emit coverage at the path the gate reads: `mvn -B clean verify` with the JaCoCo `report`
   goal bound, at 80% or more.
4. Scan dependencies: `mvn org.owasp:dependency-check-maven:check`. Address every High or
   Critical.
5. For the upload zip and the pre-upload pipeline simulation, reuse the shared, stack-agnostic
   `scripts/package-appstore.sh` and `scripts/simulate-pipeline.sh` from `templates/node/`,
   changing only the test command to the coverage command above. Run the simulation green
   before you upload.

## Pitfalls

- JaCoCo emits binary `jacoco.exec` unless the `report` goal is bound; Sonar then sees 0%.
- A hardcoded `server.port=8080` ignores the platform-injected `PORT`; use `${PORT:8080}`.
- Spring Security without `permitAll()` on the health path makes it return 401, failing the
  unauthenticated-200 contract.
