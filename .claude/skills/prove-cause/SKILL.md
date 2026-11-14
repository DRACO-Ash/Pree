---
name: prove-cause
description: Prove the root cause of a failure before any fix, retry or automation. Use when a push, API call, deploy, build or gate fails with the same error twice, or on any 401, 403 or 404.
allowed-tools: Read Grep Glob Bash(git *) Bash(gh api *) Bash(curl *)
---
# Prove the cause

1. Capture the raw error: full output, HTTP status, response headers and body where available.
2. List the layers that could have produced it: Claude environment or proxy, remote host (GitHub or GitLab rules, tokens), CI runner, registry, network, application code.
3. For each layer, run one check that would confirm or rule it out. Examples: `gh api repos/{owner}/{repo}/rulesets`, branch protection, token scopes, repository access tools, `docker login` status, rate-limit headers.
4. State the confirmed cause with the evidence that proves it, and the hypotheses ruled out with their evidence.
5. State who must act: Claude now, the user (exact commands), or CI.

Never schedule a retry for an unconfirmed cause. A retry routine stops after 2 identical failures and notifies the user.
