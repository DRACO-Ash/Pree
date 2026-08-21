# Python container scaffold templates (App Store, quality-gated)

Copy-ready files for a Python (FastAPI or Flask) server app targeting the Bluestaq App Store
container template. They materialise the `deploy-recipes` Python recipe as real files you copy,
so the App Store contract holds from the first commit instead of being prose you must remember.

## The files

- **`Dockerfile`** - hardened, multi-stage image. The venv is built in the first stage so the
  package manager never reaches the final image; suid bits are stripped and the app runs as a
  numeric non-root user. Copy to the repository root and replace every `<pinned-digest>`.
- **`sonar-project.properties`** - Code Quality gate scoping. Coverage is Cobertura XML at
  `coverage.xml`, read via `sonar.python.coverage.reportPaths`. Commit at the repository root.

## How to use

1. Copy the `Dockerfile` and `sonar-project.properties` to the repository root. Replace every
   `<pinned-digest>` with the real base-image digest and `CHANGE_ME_project_key` with your key.
2. Make the app honour the runtime contract: bind `0.0.0.0`, read `PORT` (default 8080), and
   return HTTP 200 unauthenticated at `GET /` and a health path (for example `/healthz`). The
   `Dockerfile` passes `-b 0.0.0.0:${PORT:-8080}`; uvicorn and gunicorn default to `127.0.0.1`,
   so this line is load-bearing. Never set `ENV PORT` in the Dockerfile.
3. Emit coverage in the format and at the path the gate reads:
   `coverage run -m pytest && coverage xml` (or `pytest --cov=app --cov-report=xml`), at 80%
   or more. Only the `xml` report produces the Cobertura file Sonar consumes.
4. Scan dependencies: `pip-audit -r requirements.txt`. Address every High or Critical.
5. For the upload zip and the pre-upload pipeline simulation, reuse the shared, stack-agnostic
   `scripts/package-appstore.sh` and `scripts/simulate-pipeline.sh` from `templates/node/`,
   changing only the test command to the coverage command above. Run the simulation green
   before you upload.

## Pitfalls

- uvicorn and gunicorn default to `127.0.0.1`; you must pass `-b 0.0.0.0:${PORT}`.
- Only the `xml` coverage report produces the Cobertura file; a bare `pytest` writes nothing
  the gate can read, so coverage shows 0%.
- Omitting `exec` in the launch command means SIGTERM never reaches gunicorn and shutdown hangs.
