---
name: ci-cd
description: Continuous integration that mirrors the local loop. Use when adding or changing a pull-request pipeline. Covers a PR-gating workflow that runs the same checks as the local loop, a pinned runtime, browser install for the smoke test, least-privilege permissions, and the rule that the App Store generates its own pipeline you never edit.
---

# CI/CD

> Stack note: the commands shown here are the Node example. For Python, Java, Go, Rust, or .NET, run the equivalent canonical step from `toolchain-adapters`. The principle in this skill is what binds; the command is illustrative.

## Purpose and scope

The continuous-integration (CI) pipeline that gates pull requests by running exactly what the local loop runs, so nothing reaches the default branch unverified. Scope is the PR-gating workflow in the repository. It does not cover the deploy itself (`release-and-deploy`) or the App Store's own pipeline, which the platform generates and you never edit (`app-store-deployment`).

## When to use

- Adding or changing the PR workflow.
- A check passes locally but you want it enforced on every merge request.

## Prerequisites

- `testing-standards` (the checks CI runs), `dependencies` (`npm ci`), `environment-setup` (the pinned runtime).

## Procedure

1. **Mirror the local loop in CI.** The workflow runs the same commands a developer runs: install, then the archetype's verification.
   - Static: `npm ci`, install the browser driver, `npm test` (validate, render-check desktop and mobile, static-checks).
   - Server: `npm ci`, `npm test` (unit with coverage), and the Playwright smoke test, with the browser installed.
2. **Pin the runtime.** Use the same Node major the project targets (`environment-setup`); do not float it.
3. **Install the browser for the render-check or smoke test.** `npx playwright install --with-deps chromium` in CI.
4. **Grant least privilege.** Set `permissions: contents: read` by default; add a narrower scope only where a job needs it. CI never holds a deploy secret it does not use.
5. **Gate the merge.** A red pipeline blocks the merge; do not merge past a failing required check.

## Per-stack CI templates (copy-ready)

Each is a pull-request gate that mirrors the stack's local loop: pin the runtime, install from the lockfile, test with coverage to the path the App Store SonarQube gate reads (`deploy-recipes`), and scan dependencies. All use least-privilege `permissions: contents: read`. A static-html project uses the Node template (its loop is `npm test`); the server stacks follow these. Copy the one for your stack to `.github/workflows/verify.yml` in your repo root.

### Python
```yaml
name: verify
on: { push: {}, pull_request: { branches: [main] } }
permissions: { contents: read }
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install --require-hashes -r requirements.txt
      - run: pytest --cov --cov-report=xml          # coverage.xml (Cobertura)
      - run: pip install pip-audit && pip-audit -r requirements.txt
```

### Java (Spring Boot, Maven)
```yaml
name: verify
on: { push: {}, pull_request: { branches: [main] } }
permissions: { contents: read }
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with: { distribution: temurin, java-version: "21", cache: maven }
      - run: mvn -B clean verify          # JaCoCo report at target/site/jacoco/jacoco.xml
      - run: mvn -B org.owasp:dependency-check-maven:check
```

### Go
```yaml
name: verify
on: { push: {}, pull_request: { branches: [main] } }
permissions: { contents: read }
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with: { go-version: "1.23" }
      - run: go build ./...
      - run: go vet ./...
      - run: go test ./... -coverprofile=coverage.out -covermode=atomic
      - run: go install golang.org/x/vuln/cmd/govulncheck@latest && govulncheck ./...
```

### Rust
```yaml
name: verify
on: { push: {}, pull_request: { branches: [main] } }
permissions: { contents: read }
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
        with: { components: "clippy,llvm-tools-preview" }
      - run: cargo build --locked
      - run: cargo clippy -- -D warnings
      - run: cargo install cargo-llvm-cov cargo-audit
      - run: cargo llvm-cov --lcov --output-path coverage/lcov.info
      - run: cargo audit
```

### .NET (ASP.NET Core)
```yaml
name: verify
on: { push: {}, pull_request: { branches: [main] } }
permissions: { contents: read }
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-dotnet@v4
        with: { dotnet-version: "8.0.x" }
      - run: dotnet restore --locked-mode
      - run: dotnet test --collect:"XPlat Code Coverage" -p:CoverletOutputFormat=opencover
      - run: dotnet list package --vulnerable --include-transitive
```

### Node (server or built-static)
```yaml
name: verify
on: { push: {}, pull_request: { branches: [main] } }
permissions: { contents: read }
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "22" }
      - run: npm ci
      - run: npx playwright install --with-deps chromium   # if a render or smoke test is used
      - run: npm test
      - run: npm audit --omit=dev
```

## Decision rules

- **Add a new local check?** Add it to CI in the same change, so local and CI never drift.
- **A check cannot run locally (browser absent)?** Let CI run it and treat CI as the source of truth for that check.
- **Firing on pull requests only?** Add branch pushes too (`on: { push: {}, pull_request: {...} }`). A pipeline that fires only on a pull request never runs while you work on a branch with no PR open, so a break stays invisible until you open the PR or merge. On one project CI sat red for four consecutive runs because nothing fired on a branch push, and worse, a browser leg and a pipeline-simulation step added to CI had never executed on a runner even once. A guard nobody runs is not a guard: before trusting any CI step, confirm the last time it actually ran on a runner and what it saw. Firing on every push makes a break show in minutes, not after four merges.
- **CI too slow or costly?** Cut the waste, keep the depth. Cache what is re-fetched every run, the dependency download and the browser binary (`actions/cache` on `~/.cache/ms-playwright`, keyed by the lockfile hash), so each is downloaded once not every run. Run the deterministic legs (validate, unit tests, static greps) on every change, but gate the one heavy leg, the headless render or smoke test, behind a path filter so it runs only when its input changed. Scope the filter to the leg's whole input, not just the artifact: for the static archetype that is `index.html` and the render-check script itself, so a fix to the test is actually exercised by the run that ships it. Fail safe: when the diff cannot be determined, run the heavy leg. Same checks, same coverage, less time and resource.
- **Tempted to edit the App Store pipeline?** Do not; the platform generates its own `.gitlab-ci.yml` from the template, commits it into the checkout of your uploaded zip, sets `GITLAB_CI=true`, and runs it. You never hand-edit it. Pipelines run on merge requests; create one with `git push -o merge_request.create`.
- **About to upload to the App Store?** Your repository CI passing does not prove the platform pipeline will pass; the platform runs your tests against the uploaded zip in its own environment. Simulate it first against the actual artefact, with the platform's added file and `GITLAB_CI=true` present, and confirm the coverage report the SonarQube gate reads is emitted (`appstore-gate-compliance`).
- **Deploying through the App Store's SonarQube gate?** Run the same analyser locally or in CI that the platform runs, with the same profile (cognitive complexity, loop shapes, ARIA and contrast rules), on every commit. Wired at scaffold time the violation count stays at zero; discovered at upload time it is hundreds at once. The one gate the simulation cannot reproduce is Sonar's server-side ruleset, so this per-commit analyser is its compensating control. Two calibration facts: the profile reveals rule classes progressively across scans (budget three to five upload cycles when retrofitting, or run a comprehensive modern profile such as the unicorn and sonarjs ESLint sets from day one); and a findings report is a sample, not the population, so fix by rule class and grep the whole tree to zero rather than fixing only the reported lines (`appstore-gate-compliance`).
- **Which rules does the gate actually enforce, and what does a local scan miss?** The gate is scoped to NEW CODE (Sonar's clean-as-you-code model): a smell on a line you never touch is not counted, but the instant you MODIFY that line the latent smell becomes a counted new-code violation. So scan the FULL ruleset over every file the diff changes and clean each finding on an added or modified line in the same change; a refactor that "only moves code" resurrects every latent smell on every line it touches. Two rules no local scan will catch on its own: `no-duplicate-string` (S1192) is turned OFF in `eslint-plugin-sonarjs` at its `recommended` preset yet the server enforces it, so run the FULL sonarjs set, not `recommended`; and nested-function hoisting (S7721) is not implemented by any local eslint plugin, so check it by reading. The recurring gate-failing classes, with a detect strategy, are worth encoding in a committed `compliance-check.js` run by the local loop:
  - **S1192 no duplicate string literals** - the same non-trivial literal (inner length 10 or more) appears three or more times in a file; name a constant. Prose inside a template literal does not count.
  - **S7721 nested function to the outer scope** - a function nested inside another whose body uses none of the parent's params or locals must be hoisted to module scope. Keep it nested only if it genuinely closes over a parent local. Needs scope analysis (read it); no local plugin flags it.
  - **S5852 slow (Denial of Service, DoS) regex [security hotspot]** - nested quantifiers, overlapping alternations, or anchored `+`/`*` alternations (for example `/^["']+|["']+$/`). The safest cure is to remove the regex and do the work with plain string ops, so the scanner has nothing to flag. A simple control-character class is not flagged; the backtracking shape is what matters.
  - **prefer `codePointAt` over `charCodeAt` [reliability]** - flag any `.charCodeAt(`.
  - **S6582 prefer optional chaining** - `x && x.y` becomes `x?.y`. Not `req.body && typeof req.body.x === "string"` (a `typeof`, not a member access of the left operand).
  - **S3776 cognitive complexity at most 15** - extract helpers to flatten nested control flow and long boolean sequences.
  - **S107 at most 7 parameters** - pass an options object beyond that.
  - **S4790 weak hashing [security hotspot]** - `sha1`/`md5` raise a hotspot even for a non-cryptographic fingerprint; prefer `sha256`.
  - **S5122 permissive CORS [security hotspot]** - a literal `"*"` on `Access-Control-Allow-Origin` can fire; setting it from a variable locked to the app origin does not.
  - **Also gate-failing on new lines:** `node:` protocol on built-in imports (`require("node:fs")`); object spread over `Object.assign({}, x)`; `RegExp.exec()` over `String#match()` when only testing, and `String#replaceAll("lit", "")` over a global regex for a literal; no empty `catch` (comment or handle); no reassigning a `for` counter in the body (use a `while` cursor); and no nesting more than three control-flow statements (S134, extract a helper).
- **A secret needed in CI?** Only if a job uses it; inject it as a masked CI variable, never commit it.
- **Not a Node project?** The workflow installs the stack's pinned runtime and runs the stack's locked install and test-with-coverage instead of `npm ci`/`npm test` (`toolchain-adapters`); the container build follows the stack recipe (`deploy-recipes`). The principle is unchanged: CI runs exactly what the local loop runs.

## Standards (checkable assertions)

- The CI workflow runs the same checks as the local loop, on the pinned runtime, for the project's stack (`toolchain-adapters`).
- The browser is installed in CI for the render-check or smoke test.
- Default workflow permissions are `contents: read`.
- A failing required check blocks the merge.
- No App Store pipeline file is hand-edited in the repo.
- A non-Node project copies the matching per-stack template above to `.github/workflows/verify.yml`, so it inherits a ready pipeline.

## Failure modes and remedies

- **CI passes but local fails (or vice versa).** Cause: drift between the two. Fix: make CI run the identical commands; pin the runtime in both.
- **Smoke test fails only in CI.** Cause: browser not installed. Fix: add the install step.
- **A workflow has write-all permissions.** Fix: scope down to `contents: read` and add narrow grants only where needed.
- **Someone edits the generated platform pipeline.** Fix: revert; configure through the App Store, not the file.

## Verification

Open a pull request: the workflow runs install and the archetype's checks on the pinned runtime, installs the browser, and the merge is blocked while any required check is red. Inspect the workflow file: default permissions are `contents: read`.

## Worked example

A static project's PR workflow runs `npm ci`, `npx playwright install --with-deps chromium`, then `npm test`. A change that introduces a console error fails the render-check in CI, the required check goes red, and the merge button is disabled until the author fixes it and the pipeline goes green. The App Store pipeline is untouched; it is generated by the platform at submission time.

## Glossary

- **CI/CD:** Continuous Integration and Continuous Deployment; automated build, test, and release.
- **Least privilege:** granting a job only the access it needs (`contents: read` by default).
- **Merge request / pull request:** the change proposal CI gates before it reaches the default branch.
- Other terms: `glossary`.

## Provenance

Merged from both bundles' CI workflows (mirror-the-local-loop, pinned runtime, browser install, least-privilege permissions) and the App Store doctrine that the platform generates its own pipeline you never edit (`appstore.md`).

## Field lesson: require the whole loop green before merge

The deterministic loop passing is necessary, not sufficient. Protect the default branch so the full verify job, including the browser render-check, must be green before a merge. Merging on the fast subset alone lets a red browser leg reach the protected branch, which happened repeatedly during the Launchpad build until the rule was enforced. One required status check on the real pipeline closes the gap.
