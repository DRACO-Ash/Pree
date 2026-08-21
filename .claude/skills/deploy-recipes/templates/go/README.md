# Go container scaffold templates (App Store, quality-gated)

Copy-ready files for a Go server app targeting the Bluestaq App Store container template. They
materialise the `deploy-recipes` Go recipe as real files you copy, so the App Store contract
holds from the first commit instead of being prose you must remember.

## The files

- **`Dockerfile`** - hardened, multi-stage image: a static CGO-free binary on a distroless
  nonroot base, so the final image has no shell, no package manager, and a numeric non-root
  user. Copy to the repository root, replace every `<pinned-digest>`, and adjust `./cmd/server`
  to your main package path.
- **`sonar-project.properties`** - Code Quality gate scoping. Coverage is the native Go
  profile at `coverage.out`, read via `sonar.go.coverage.reportPaths`. Commit at the root.

## How to use

1. Copy the `Dockerfile` and `sonar-project.properties` to the repository root. Replace every
   `<pinned-digest>` and `CHANGE_ME_project_key`.
2. Make the app honour the runtime contract, for example:
   `port := os.Getenv("PORT"); if port == "" { port = "8080" }; http.ListenAndServe("0.0.0.0:"+port, mux)`,
   with `/` and `/health` returning 200 unauthenticated. Never set `ENV PORT` in the Dockerfile.
3. Emit coverage at the path the gate reads:
   `go test ./... -coverprofile=coverage.out -covermode=atomic`, at 80% or more. Sonar reads
   the profile natively; convert with `gcov2lcov` only if your gate insists on lcov.
4. Scan dependencies: `govulncheck ./...`. Address every High or Critical.
5. For the upload zip and the pre-upload pipeline simulation, reuse the shared, stack-agnostic
   `scripts/package-appstore.sh` and `scripts/simulate-pipeline.sh` from `templates/node/`,
   changing only the test command to the coverage command above. Run the simulation green
   before you upload.

## Pitfalls

- Never bind `localhost`; bind `0.0.0.0` or the pod is unreachable and goes Degraded with no log.
- `http.FileServer` and `gorilla/mux` `StrictSlash(true)` 301-redirect `/`, breaking the
  unauthenticated-200 health contract; add an explicit health route.
- Always default an empty `PORT` to 8080.
