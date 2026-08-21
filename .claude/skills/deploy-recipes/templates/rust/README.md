# Rust container scaffold templates (App Store, quality-gated)

Copy-ready files for a Rust (axum or actix) server app targeting the Bluestaq App Store
container template. They materialise the `deploy-recipes` Rust recipe as real files you copy,
so the App Store contract holds from the first commit instead of being prose you must remember.

## The files

- **`Dockerfile`** - hardened, multi-stage image: dependencies cache first, then a release
  build locked to `Cargo.lock`, shipped on a distroless `cc` (glibc) nonroot base. Copy to the
  repository root, replace every `<pinned-digest>`, and adjust `server` to your binary name.
- **`sonar-project.properties`** - Code Quality gate scoping. Coverage is lcov at
  `coverage/lcov.info` (Sonar has no first-class Rust importer; see the file's note). Commit at
  the repository root.

## How to use

1. Copy the `Dockerfile` and `sonar-project.properties` to the repository root. Replace every
   `<pinned-digest>` and `CHANGE_ME_project_key`.
2. Make the app honour the runtime contract: read `PORT` (default 8080), bind `0.0.0.0`, and
   put no auth middleware on `/` and `/health` so they return 200. Never set `ENV PORT` in the
   Dockerfile.
3. Emit coverage: `cargo llvm-cov --lcov --output-path coverage/lcov.info` (needs the
   `llvm-tools-preview` component), at 80% or more. lcov is the portable target.
4. Scan dependencies: `cargo audit`. Address every High or Critical.
5. For the upload zip and the pre-upload pipeline simulation, reuse the shared, stack-agnostic
   `scripts/package-appstore.sh` and `scripts/simulate-pipeline.sh` from `templates/node/`,
   changing only the test command to the coverage command above. Run the simulation green
   before you upload.

## Pitfalls

- A default (glibc) build on `distroless/static` fails to start; use the `cc` base (as here)
  or build a musl static binary and switch to `static`.
- Bind `0.0.0.0`, not `127.0.0.1`, or the pod is unreachable.
- Without the `llvm-tools-preview` component, coverage generation silently yields an empty
  `lcov.info` and the gate reads 0%.
