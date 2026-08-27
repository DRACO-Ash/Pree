# Beyond the gate

What the nine stages do not see, what to put in place instead, and the obligations a green pipeline does not discharge.

## Contents

1. The gap register
2. Standing risks in the current posture
3. The 2026 threat picture
4. Controls worth adopting, in order of value
5. The regulatory map: US, EU, UK, NATO
6. What to say when someone claims a green pipeline means secure

## 1. The gap register

A gate that has never fired tells you almost nothing about where its threshold sits. Five supply-chain-related stages have passed on every PSIRENS upload, including the two uploads that failed elsewhere. That is not evidence of a healthy dependency set. It is evidence that these gates have not yet had anything to say to us.

| Stage | What it does not see | Cover it with |
|---|---|---|
| 1 Secret Detection | Secrets already in git history; secrets in an image layer that a later layer deletes; credentials in a CI variable | Allowlist packaging (already in place), history scanning, BuildKit secrets rather than `ENV`/`COPY` |
| 2 Dependencies | Undocumented. Nobody has read its output | Read a job log and convert this line into a fact |
| 3 SAST Scan | Unknown rule set and threshold | `ruff check --select S` locally, which is the Bandit rule set, plus `mypy` strict |
| 4 Dependency Scanning | The runtime-only lock file; malicious-but-undisclosed packages; anything outside the advisory database; whether the vulnerable function is reachable | `pip-audit` on all three lock files; delayed ingestion; static reachability; your own SBOM |
| 5 Test | Whether the tests are any good | Coverage floor, mutation testing on high-risk modules |
| 6 Code Quality | Security, mostly. It is a maintainability gate | Treat separately; see the SonarQube discipline in `CLAUDE.md` |
| 7 Dockerfile Lint | Whether the base image is pinned by digest; whether the patch step actually ran | `hadolint` locally plus the digest-drift assertion in the loop |
| 8 Container Build | Reproducibility. Two builds of the identical archive can differ | Digest-pinned base, `--require-hashes` install, scheduled rebuild |
| 9 Container Scan | Nothing about provenance. It matches packages against a database | Sign and attest the image; keep an SBOM of the image, not just the source |
| All | Whether the gate ran at all, honestly | The four rules in `verification-loop.md` |

The single largest gap, restated because it is counter-intuitive: **stage 4 scans `requirements.txt`, which is the test-inclusive superset, and never reads `requirements-runtime.txt`, which is what the image installs.** Today that is safe by coincidence of subset identity, not by control.

## 2. Standing risks in the current posture

None of these has failed a gate. All are observable today, and all are the kind of thing a supply-chain gate eventually finds.

### 2.1 The base image is pinned by tag, not by digest

**FACT.** The Dockerfile builds `FROM python:3.12-slim` in two stages, and its own header comment says to pin a real digest at build time. The digest was never substituted.

**INFERENCE.** `python:3.12-slim` is a moving tag, so two builds of the identical archive can produce different images with different operating system package sets and different Container Scan results. This is the largest reproducibility gap in the build and the first item to fix. Current industry practice is unambiguous: pin by SHA256 digest, not by tag, so the image you scan is the image you ship.

**The obligation digest pinning creates.** A pinned digest never patches itself. Pair it with a scheduled job, weekly is a common cadence, that refreshes the digest, rebuilds, re-scans, and opens a merge request if the scan passes. Rebuilding on a schedule rather than only when application code changes is what keeps a clean image clean.

### 2.2 The final image contains a whole Debian filesystem

**FACT.** The last stage is `FROM scratch` followed by `COPY --from=prep / /`.

**INFERENCE.** The `scratch` base is a layer-flattening device, chosen so the image-policy scanner finds no setuid or setgid bit in layer history. It is not a minimal image: the entire Debian userland is copied in, so stage 9 sees every operating system package, not just the Python ones.

**The available improvement.** Moving from a full distribution base to a minimal or distroless one routinely drops scanner-reported CVE counts by a large margin without changing a line of application code. Distroless images carry libc, CA certificates and timezone data, with no shell and no package manager. That is a material change to the image-policy behaviour and to the flatten trick, so it needs testing rather than adopting on trust, but it is the highest-value structural change available at stage 9.

### 2.3 Operating system patching is fail-open

**FACT.** The patch step ends `... || true`.

**INFERENCE.** Deliberate, so a transient mirror failure cannot break the build. The cost is that it can silently no-op: a build where `apt-get upgrade` failed outright looks identical to one where it succeeded, and the image carries unpatched packages into stage 9. If Container Scan ever fails on an operating system package, check this first, because a green build is not evidence the patch ran.

This is precisely the failure mode Rule 1 and Rule 4 of the local loop exist to prevent, surviving in a file the guard does not cover. Either remove the `|| true` and accept build fragility, or emit a distinguishable marker on the failure path so the two outcomes are not identical in the log.

### 2.4 Pinning makes you stationary, not safe

**FACT.** All 19 PSIRENS runtime dependencies use `==`; zero use ranges.

**INFERENCE.** Exact pinning makes a scan result reproducible between uploads: the same archive scans the same way twice, and a new finding means the advisory database moved, not that the resolver drifted. But the set ages every day it is not touched. A quiet gate today is evidence that nothing in that set has been disclosed **yet**.

### 2.5 A small dependency set is the cheapest control you have

**FACT.** `sgp4==2.27` is the only PSIRENS dependency added for the application's own purposes, recorded as a deliberate choice. Everything else is FastAPI, Pydantic, Starlette, uvicorn, gunicorn, httpx and their transitive closure.

**INFERENCE.** The convention of no new runtime dependency without a recorded reason is doing real work and should be defended. A typical Python project carries dozens of transitive dependencies nobody chose; every one you decline to add is an attack path you never have to defend.

## 3. The 2026 threat picture

Context for why the controls below are worth their cost. All from published incident analysis rather than from any Bluestaq observation.

● **Credential theft beats vulnerability exploitation.** The GhostAction campaign in September 2025 injected code into CI workflows across more than 570 repositories and stole in excess of 3,300 secrets, including PyPI and npm tokens and cloud keys. The Shai-Hulud worm in November 2025 was cross-ecosystem and self-replicating, reaching PyPI because monorepos store credentials for both registries. Neither needed a CVE.
● **Maintainer account takeover.** The Ultralytics compromise in December 2024 injected a miner into four versions of a package downloaded tens of millions of times a month, via a workflow injection that stole the upload token. The `ctx` incident in 2022 worked by re-registering a maintainer's expired email domain.
● **Phishing that defeats time-based two-factor.** The July 2025 campaign against PyPI maintainers used a proxy credential harvester that passed stolen credentials to the real site, so victims believed they had logged in normally.
● **Slopsquatting.** Language models hallucinate plausible package names at a material rate, the same names recur across runs, and attackers register them. Unlike typosquatting these are not misspellings, so registry similarity heuristics never fire. This matters directly to us: any dependency name that arrived from an AI-assisted workflow must be verified against the registry and against its actual project, not accepted because it looked right. Autonomous agents that can install packages without confirmation remove the human checkpoint entirely.
● **Third-party involvement in breaches is rising**, roughly doubling year on year to around 30 per cent by the 2025 Verizon data, and remediation is not keeping pace.

The common thread: none of these is a CVE in a pinned package, and therefore none of them is something stage 4 would ever have found.

## 4. Controls worth adopting, in order of value

Ordered by value per unit of effort. Items 1 to 4 are cheap and should be standing practice.

1. **Ruff security rules in the loop.** One line of configuration, sub-second runtime, catches hardcoded secrets, weak hashes, missing timeouts, unsafe deserialisation and SQL string formatting before commit.
2. **Hash-locked dependencies compiled for the exact interpreter version**, installed with `--require-hashes --no-deps`. Already in place in Enlightenment; make it universal. Remember that `pip` only enforces hashes when every line has one.
3. **Base image pinned by digest, with a scheduled refresh job.** Closes the largest reproducibility gap and the largest stage 9 surface at once.
4. **Your own CycloneDX SBOM, generated at build time and versioned alongside the code.** The platform generates one but does not hand it to you in a form you control. You need your own for incident response, for CRA technical documentation, and for customer assurance. Generate it from the lock file rather than from an installed environment, so it is reproducible.
5. **Delayed ingestion.** `uv pip compile --exclude-newer <date one week back>` lets the wider community be the canary for obviously malicious releases. Cheap for a single project. Not a guarantee: it will not catch a sophisticated compromise, and it delays security patches, so keep an expedite path.
6. **Static reachability**, where the platform supports it for pip. Marks which SBOM components your code actually imports, so triage can start with the ones that matter. Reduces noise; does not reduce risk.
7. **Provenance and signing.** Sigstore-based attestation of the built image, and, if we ever publish Python artefacts, Trusted Publishing over OIDC rather than long-lived API tokens. This is the control that would have contained GhostAction and Shai-Hulud. Note that OIDC alone is not a silver bullet: anyone who can modify the workflow can trigger a legitimate token exchange, so pin actions by commit SHA and require review on the publish path.
8. **A private index or pull-through cache with a single `index-url`.** Prevents dependency confusion, where a public package with the same name and a higher version wins the resolution. Note that resolution jobs on the platform honour `PIP_INDEX_URL` and `PIP_EXTRA_INDEX_URL`, so this has to be set where developers cannot casually override it.
9. **VEX statements** for advisories you have assessed as not exploitable in your build. See `optional-testing.md`.

## 5. The regulatory map: US, EU, UK, NATO

The App Store gates are input by Bluestaq LLC, a US entity. Nothing in them discharges any of the following, and the evidence they produce is not in a form any of these accept.

### United States

● **NIST SP 800-218, the Secure Software Development Framework.** The reference framework behind federal software procurement attestation. The loop in `verification-loop.md` maps to it directly: PW.4 on reusing well-secured software, PW.7 and PW.8 on review and testing, RV.1 on identifying and confirming vulnerabilities.
● **CISA 2026 Minimum Elements for a Software Bill of Materials**, released July 2026 jointly with NSA, FBI and international partners including the UK, replacing the 2021 NTIA baseline. New fields relative to 2021 include component hash algorithm, component licence, SBOM tool name, and SBOM generation context. It applies to all software, including open source, AI software and software as a service. Two practical consequences for us: generate SBOMs with a tool that records its own identity and the generation context, and include component hashes, which our lock files already carry and which a CycloneDX generator can emit.
● **NIST SP 800-171** where controlled unclassified information is in scope, which for defence work it usually is.

### European Union

● **The Cyber Resilience Act, Regulation (EU) 2024/2847.** Two dates matter and they are routinely conflated.
  ● **11 September 2026**: vulnerability and incident reporting obligations bind. Manufacturers must report actively exploited vulnerabilities and severe incidents to ENISA and the designated national CSIRT, with a 24-hour early warning from the moment of awareness, through the ENISA Single Reporting Platform. This applies to products already shipped.
  ● **11 December 2027**: full application, including essential requirements, conformity assessment, CE marking, and technical documentation. Annex I Part II point 1 requires an SBOM in a commonly used machine-readable format, covering at least top-level dependencies.
● **The dependency between the two dates is the operational point.** A 24-hour early-warning window leaves no time for manual component archaeology. Answering "which of our products and versions contain this component" in minutes requires current machine-readable SBOMs for every shipped version, matched continuously against vulnerability intelligence. In practice SBOM readiness is required from September 2026, fifteen months before the SBOM deadline itself.
● Penalties run to €15 million or 2.5 per cent of global annual turnover, whichever is higher.
● **NIS2** runs in parallel for entities classified as essential or important. A single incident can trigger reporting under multiple frameworks with different clocks and different recipients. Map those obligations before the first incident, not after.

### United Kingdom

● **The Software Security Code of Practice** is voluntary guidance rather than binding law, but it is the reference customers will use, and its expectations align with the controls in section 4.
● Our own IASME Cyber Assurance and Defence Cyber Certification evidence draws on the same artefacts. See the `evidence-pack-assembly` and `iasme-portal-drafting` skills; the loop's outputs are directly citable there.

### NATO and allied

Where UDL, GDM or ARQ components are delivered into allied programmes, expect SBOM and vulnerability-handling questions to arrive through supplier assurance rather than through a regulation. Have the SBOM, the audit evidence and the suppression justifications ready as a package rather than assembling them under time pressure. See `supplier-assurance`.

## 6. What to say when someone claims a green pipeline means secure

Five gates concerned with supply chain have passed on every upload. That is a fact. What follows from it is narrow, and worth stating plainly rather than letting the inference run:

● These gates pass because the dependency set is small, exactly pinned, framework-shaped, and split so the runtime image carries no test tooling. Those are real controls and they should be defended deliberately.
● None of it means the gates are weak, or that the application is secure. It means these gates have not yet had anything to say to us.
● The three items in section 2 (an unpinned base image digest, a fail-open patch step, and a full Debian userland in the final image) are where the first real finding is most likely to appear, and none of them is visible from a passing pipeline.
● The threats in section 3 are the ones most likely to actually hurt us, and stage 4 would not detect any of them.
