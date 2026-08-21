---
description: Generate a gate-compliant App Store starting repository and take it to a green verification loop, instead of only describing one. Decides the archetype, materialises the compliant tree from the shipped per-stack templates (Node, Python, Go, Java, Rust, .NET copied verbatim), wires the port, health, coverage, and sonar contract, and runs the loop until it passes. Scaffolds and verifies only; deploy stays human-gated.
argument-hint: [stack or archetype hint, e.g. "node server", "python api", "static"]
---
Use the `getting-started`, `deploy-recipes`, `appstore-gate-compliance`, and `toolchain-adapters` skills to
scaffold a compliant App Store project in this repository and take it to green.

Stack or archetype hint (may be empty): $ARGUMENTS

Work in this order:

1. Decide the shape. Run the `getting-started` Step 0 archetype decision (static single file, or server
   container) and detect the App Store template. Record the archetype and template in `CLAUDE.md`.

2. Materialise, do not narrate. Lay down the actual compliant tree, do not just describe it.
   - Copy your stack's template set from `deploy-recipes/templates/<stack>/` verbatim, never re-derive it
     (a checklist is a liability until it is materialised). Node ships the fullest set (Dockerfile,
     run-tests.mjs, sonar-project.properties, eslint.config.mjs, package-appstore.sh, simulate-pipeline.sh,
     README); Java, Python, Go, Rust and .NET each ship a hardened Dockerfile, the stack's
     sonar-project.properties, and a copy-ready README (Java also application.properties), and reuse Node's
     stack-agnostic package-appstore.sh and simulate-pipeline.sh. Replace every `<pinned-digest>` and
     `CHANGE_ME` and adjust the marked lines to your layout.
   - Every stack: apply the `appstore-gate-compliance` build-compliant-from-start checklist. Read
     `process.env.PORT` defaulting to 8080, bind `0.0.0.0`, never set `ENV PORT`. Answer `/` and the health
     paths with 200, unauthenticated, touching nothing. Put `sonar-project.properties` and the Dockerfile at
     the repo root. Add `.dockerignore`. Fill `CLAUDE.md` from its template. Keep secrets server-side only.

3. Take it to green. Run the verification loop (`npm test`, or the stack equivalent per `toolchain-adapters`)
   and iterate until it passes with coverage at or above eighty per cent. Report the green result with the
   command output.

4. Stop at green. Build, package, and deploy remain human-gated: they run through `release-and-deploy` and
   the `deploy-gate` on an explicit human go-ahead. This command scaffolds and verifies; it never deploys.

Scope note: every supported stack ships real template files under `deploy-recipes/templates/<stack>/`; copy
your stack's set and adjust the marked lines rather than writing them from memory. Hold the house voice in
anything you write.
