# Node container scaffold templates (App Store, quality-gated)

Copy-ready files for a Node server app targeting the Bluestaq App Store container template.
They exist so the `appstore-gate-compliance` "scaffold-time defaults" are real files you
copy, not prose you must remember to write. The retrospective that produced them found that
a green local loop and passing reviews still failed the platform because the scaffold shipped
a conventional Dockerfile and a bare `node --test`. These templates close that gap from the
first commit.

## The files

- **`Dockerfile`** - hardened, flattened, `FROM scratch` multi-stage image. Passes the image
  policy scan by construction (no package manager, no suid/sgid bits, one clean layer). Copy
  to the repository root.
- **`run-tests.mjs`** - the `test` script. Tolerates the platform's `npm test -- --coverage`,
  scopes discovery with an explicit glob, and always emits `coverage/lcov.info`.
- **`sonar-project.properties`** - Code Quality gate scoping (sources, tests, the lcov path,
  coverage exclusions). Commit at the repository root.
- **`eslint.config.mjs`** - a Sonar-equivalent lint profile, so violations are fixed one at a
  time locally instead of six hundred at once on the first upload.
- **`package-appstore.sh`** - builds the testable source zip via `git archive` and checks the
  two contracts that most often fail on upload.
- **`simulate-pipeline.sh`** - runs the platform pipeline against the ACTUAL artefact before
  every upload. If this is not green, the upload will not be either.

## How to use

1. Copy the files to the matching locations (Dockerfile and `sonar-project.properties` at the
   root; the scripts under `scripts/`; wire `run-tests.mjs` as your `test` script).
2. Replace every `CHANGE_ME` and adjust the marked lines (glob path, sources) to your layout.
3. Add the pinned dev dependencies named in `eslint.config.mjs`.
4. Run `simulate-pipeline.sh` and make it green before you upload.

## Other stacks

Java, Python, Go, Rust, and .NET follow the same shape and are materialised the same way, each
in a sibling `templates/<stack>/` directory: a hardened Dockerfile, the stack's
`sonar-project.properties` coverage scoping, a copy-ready README, and for Java an
`application.properties`. They reuse this directory's stack-agnostic `package-appstore.sh` and
`simulate-pipeline.sh` (change only the test command to the stack's coverage command). The full
per-stack detail (Dockerfile, coverage path, scanner, pitfalls) is in the `deploy-recipes` skill
body; `/scaffold` copies the right set for you.
