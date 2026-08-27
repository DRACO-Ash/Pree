# Optional testing and assurance layers

Everything worth considering above the gate, with an honest judgement on whether it earns its cost for a given application.

## Contents

1. How to choose
2. Static reachability
3. Mutation testing
4. Property-based testing
5. Coverage-guided fuzzing
6. Second-opinion scanning
7. VEX and exploitability
8. SBOM generation, done properly
9. Workflow and pipeline auditing
10. A staged adoption plan

## 1. How to choose

Three questions decide almost every case.

● **Does this application parse untrusted input?** If yes, property-based testing and fuzzing move from optional to expected. A propagator, a message parser, a file ingester and an API boundary all qualify.
● **Is the code path load-bearing for an operational decision?** If a wrong answer would misinform a crew, mutation testing on that module is worth the CI time.
● **Will someone outside Bluestaq ask for evidence?** If yes, SBOM generation and VEX stop being optional at all, regardless of engineering value. See `beyond-the-gate.md` section 5.

Everything else is discretionary. Do not adopt a layer because it exists.

## 2. Static reachability

**What it is.** The analyser parses source files to identify which SBOM components your code actually imports, and marks them in the SBOM, so triage can start with the components that are genuinely referenced rather than merely present.

**Availability.** Supported on the platform for Python via a pip-compile `requirements.txt` lockfile and via `pipdeptree.json`. Off by default; enabled with `DS_STATIC_REACHABILITY_ENABLED`, which means a pipeline change.

**Judgement.** Worth asking for. Most known vulnerabilities in a real application live in transitive dependencies the application never calls, and open-source scanners flag every match regardless. Reachability is the cheapest available reduction in triage noise.

**The caveat that matters.** Reachability reduces noise, not risk. An unreachable component is still installed, still present in the image, and still reachable by anything else in the container. Do not let a reachability marking become a reason not to patch.

## 3. Mutation testing

**What it is.** Systematically alter the code, then check whether the test suite fails. A mutant that survives marks a test that is not really testing anything. The score is killed mutants over total mutants.

**Cost.** Roughly 20 to 30 per cent added CI time on the paths it covers, and it is intolerant of flaky or network-dependent tests.

**How to use it without it becoming theatre.**
● Run normal `pytest` and coverage on every change. Run mutation testing nightly, or on changed high-risk modules only.
● Do not set a hard 100 per cent mutation score gate. Use "no new surviving mutants in protected modules", or "the score must not drop below the reviewed baseline".
● Equivalent mutants, where the change alters syntax but not observable behaviour, are common in Python because of defensive guards and normalised data. Discuss them; do not hide them. Sometimes an equivalent mutant is a design smell and simplifying the production code is the better answer than adding a test.
● Never write a test that asserts implementation trivia purely to kill a mutant. That makes the suite worse.
● Store the output as a CI artefact so a developer can see the survivor without reproducing the whole run.

**Where it earns its place here.** Standard-library-only tooling that is outside the coverage gate and outside `sonar.sources` has no automated signal at all. Mutation testing by hand at the review gates is currently the only check on those files, and automating it for exactly those files is a straightforward win.

## 4. Property-based testing

**What it is.** Instead of asserting a specific output for a specific input, assert a property that should hold for all inputs, and let the framework generate them. When it finds a failure it shrinks the input to the minimal counterexample, which is why the failures are far easier to debug than a fuzzer's.

**Where to start.** The productive patterns are few: round-trip (encode then decode returns the original), idempotence (applying twice equals applying once), invariants (a physical quantity stays within bounds), and comparison against a slow but obviously correct reference implementation.

**Fit for our work.** Strong. Orbital propagation, coordinate transforms, time-system conversions and message encoders are exactly the shape property-based testing was built for. A round-trip property over a TLE parser or a state-vector conversion will find edge cases a hand-written suite will not.

**Cost.** Low. It is a normal `pytest` dependency and runs in the existing suite. The hard part is inventing the properties, not the API.

## 5. Coverage-guided fuzzing

**What it is.** Mutation of raw input bytes, guided by coverage feedback, to drive a parser into a crash. For Python this means a native fuzzer built on libFuzzer.

**Fit.** Only for code that parses untrusted or externally supplied binary or text formats. If the application consumes only validated JSON from an authenticated API, the return is poor.

**Known limitation.** A coverage-guided fuzzer struggles with complex structured formats, because random mutation produces inputs that are rejected at the first parse step and coverage stays low. If the format has structure, supply a seed corpus and a custom mutator, or accept that property-based testing will find more per hour spent.

**Judgement.** Third in line behind property-based testing and mutation testing for the applications we currently ship. Reconsider the moment we take in a binary telemetry or sensor format from outside.

## 6. Second-opinion scanning

`pip-audit` is the primary, and it queries OSV, which aggregates the PyPA advisory database, GitHub advisories and NVD. Coverage is strong but not exhaustive; treat it as an important signal rather than a guarantee.

Second opinions worth having, and what each is for:

| Tool | Use it for | Note |
|---|---|---|
| Trivy | One binary covering the image, the filesystem, infrastructure as code, secrets and SBOM generation | The broadest single-tool coverage; good CI gate |
| Grype | Vulnerability matching against an SBOM you already generated, with exploit-prediction and known-exploited-catalogue risk scoring | Pairs naturally with a Syft or CycloneDX SBOM |
| OSV-Scanner | Guided remediation, container layer awareness, and the same OSV data with a different extractor | Its remediation guidance does not yet cover Python as well as it covers npm and Maven |

Running Trivy in the commit gate and Grype on a weekly schedule is a reasonable division. Do not run three scanners in the blocking path; you will get three severity opinions and no decision.

**Pin the scanner action by commit SHA, not by tag.** Tags are mutable, and at least one widely used scanning action had its tags force-pushed during a supply-chain incident in March 2026.

## 7. VEX and exploitability

**What it is.** A machine-readable statement saying whether a given advisory actually affects your product. It lets a consumer's scanner stop firing on a CVE you have assessed and found not exploitable in your build. OpenVEX is the open implementation.

**Why it matters for us specifically.** Under the Cyber Resilience Act, where a product contains an actively exploited vulnerability originating in a third-party component, the manufacturer must notify. Where the vulnerability cannot be exploited in your product, the position is different, and the reasoning has to exist in writing before the 24-hour clock starts, not after. VEX is the format that reasoning belongs in.

**Practical route.** Every suppression recorded under `verification-loop.md` section 8 is a draft VEX statement. Write the justification in the repository at the time of the decision, in a form that can be mechanically converted. Adoption of VEX in the Python ecosystem is still early, so do not over-engineer the tooling; get the justifications written first.

## 8. SBOM generation, done properly

Generate from the lock file, not from an installed environment, so the result is reproducible and describes what ships rather than what happened to be present on one machine.

Requirements to satisfy the 2026 CISA minimum elements and the CRA together:

● CycloneDX, versioned in source control alongside the code, regenerated at build time.
● Component hashes included. Our lock files already carry them; make sure the generator emits them.
● Package URLs for every component, so the document joins automatically against vulnerability data.
● The generating tool's own name and the generation context recorded. These are new fields relative to the 2021 baseline and a generator that omits them will need replacing.
● External references to source repositories where available, which is what makes an incident-response search useful rather than merely complete.

One SBOM per shipped version, retained. The point of the artefact is answering "which of our products and versions contain this component" in minutes.

## 9. Workflow and pipeline auditing

The GhostAction campaign exploited workflow vulnerabilities rather than code vulnerabilities. If any part of our build lives in GitHub Actions, audit the workflows themselves for template injection, unpinned actions, over-scoped tokens and credential leakage. Tooling exists for this and runs as a pre-commit hook or a CI job.

The equivalent discipline on GitLab is the one already stated in `gate-mechanics.md` section 7: treat the dependency resolution job as a sensitive execution context, keep build-tool variables in protected scope, and prefer a lockfile generated in a job you control over letting the platform resolve for you.

## 10. A staged adoption plan

**Phase 1, one to two days, do this now.**
● Ruff security rules in the loop.
● Hash-locked dependencies compiled for the exact interpreter version.
● Base image pinned by digest, plus a scheduled digest-refresh job.
● Own CycloneDX SBOM generated at build time from the lock file.

**Phase 2, about a week.**
● Property-based tests over the parsing and conversion boundaries.
● Trivy in the commit gate, Grype weekly.
● Automated dependency update proposals, reviewed rather than auto-merged.
● Delayed ingestion via `--exclude-newer`.

**Phase 3, ongoing, and only where it earns its place.**
● Mutation testing on protected modules, nightly, with a baseline rather than a hard threshold.
● Static reachability, once a pipeline change can be made.
● VEX statements attached to every suppression.
● Fuzzing, if and when we ingest an untrusted binary format.

Start small, get the basics right, then expand. Each layer defends against a different attack path; the value is in the overlap, not in any one of them.
