#!/usr/bin/env node
// secret-scan.mjs :: PreToolUse guardrail for Write|Edit|MultiEdit.
// Reads the hook payload on stdin, scans the content being written for credential
// patterns and for banned anti-patterns (client-side access gate; Dockerfile ENV PORT),
// and BLOCKS the write (exit code 2) if any match. Deterministic: same input, same verdict.
//
// Claude Code passes a JSON payload on stdin with tool_name and tool_input.
// Exit 0 = allow. Exit 2 = block; stderr is shown to the model as the reason.

import { readFileSync } from 'node:fs';

let raw = '';
try { raw = readFileSync(0, 'utf8'); } catch { process.exit(0); }

let payload = {};
try { payload = JSON.parse(raw || '{}'); } catch { process.exit(0); }

const ti = payload.tool_input || {};
// Collect every string that could carry new content across Write/Edit/MultiEdit.
const parts = [ti.content, ti.new_string, ti.file_text];
if (Array.isArray(ti.edits)) for (const e of ti.edits) parts.push(e && e.new_string);
const text = parts.filter(s => typeof s === 'string').join('\n');
if (!text) process.exit(0);

// Each rule is a labelled pattern. Extend per project; keep each labelled.
const RULES = [
  ['AWS access key id',          /\bAKIA[0-9A-Z]{16}\b/],
  ['Generic API key assignment', /(?:api[_-]?key|secret|token|password|passwd|pwd)\s*[:=]\s*['"][^'"]{8,}['"]/i],
  ['Bearer token',               /\bBearer\s+[A-Za-z0-9._\-]{20,}\b/],
  ['Private key block',          /-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----/],
  ['LLM provider key',           /\b(sk-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,})\b/],
  ['Google API key',             /\bAIza[0-9A-Za-z_\-]{35}\b/],
  ['Slack token',                /\bxox[baprs]-[0-9A-Za-z\-]{10,}\b/],
  ['GitLab personal token',      /\bglpat-[0-9A-Za-z_\-]{20,}\b/],
  // Banned anti-pattern: a hardcoded client-side access gate (public artifact PIN).
  ['Client-side access gate',    /\b(?:ADMIN_)?PIN\s*=\s*['"][0-9A-Za-z]{4,}['"]/],
  // Banned anti-pattern: ENV PORT in a Dockerfile silently overrides the platform port 8080.
  ['Dockerfile ENV PORT',        /^\s*ENV\s+[^\n]*?(?<![A-Za-z0-9_])["']*PORT["']*\s*=/im],
  // `["']*`, not `["']?`: a DOUBLED quote is a distinct evasion, and the key may carry quotes on
  // either side. A continuation line carrying an assignment with no ENV keyword still escapes
  // every rule here, because they are anchored to the keyword. Making the keyword optional was
  // tried and reverted: it fired on `token = os.environ[...]`, on `token=require_token,` and on an
  // empty `PREE_TEAM_TOKEN=`, and a net that flags a repository's own source gets switched off.
  // The boot contract's ENV allowlist is the net that gates the continuation case in CI; this hook
  // is fast feedback at write time, not the gate.
  //
  // The generic rule above requires a QUOTED value, so `ENV PREE_TEAM_TOKEN=Ab3-Cd6...` in a
  // Dockerfile was allowed while the same line quoted was blocked. This is the assignment form a
  // baked credential actually takes, and it is matched unquoted.
  //
  // Deliberately NARROW rather than a general bare-value rule. A general one was tried and fired
  // on five legitimate files at once: the prose placeholder `token=<token>`, the sentence
  // "Token: anything", and the keyword argument `token=require_token,`. A net that flags a
  // repository's own documentation gets switched off rather than obeyed, so the name must END in
  // a credential term after a separator, which keeps MONKEY out of it.
  // The term may sit ANYWHERE in the name, not only at the end: `ENV TEAM_TOKEN_VALUE=`,
  // `ENV PREE_TOKEN_2=` and `ENV PREE_TOKEN_FILE=` all walked past a name-must-end-in-term rule.
  // `PASSWD` and `PWD` are here because the generic rule above has known them all along and this
  // one did not, so `ENV DB_PASSWD=...` was allowed by both: a real shape for a UDL integration,
  // where the credential is a password. The value floor is 4, not 8, because `abc123` is a
  // credential too. Measured across every tracked file at this width: no new match.
  ['Baked credential assignment', /^\s*(?:ENV|ARG|export)\s+[^\n]*?(?<![A-Za-z0-9_])["']*(?:[A-Za-z0-9]+_)*(?:TOKEN|SECRET|PASSWORD|PASSWD|PWD|PASSPHRASE|KEY|CREDENTIAL)(?:_[A-Za-z0-9]+)*["']*\s*=\s*\S{4,}/im],
  // Banned anti-pattern: any platform-injected variable given an image-level default. PREE_ENV is
  // the worst of them, because the loader defaults it to production, so baking `development`
  // turns off the token requirement, serves the docs unauthenticated and admits a cleartext
  // origin.
  ['Dockerfile ENV platform value', /^\s*ENV\s+[^\n]*?(?<![A-Za-z0-9_])["']*(?:PREE_ENV|PREE_DATA_DIR|PREE_TEAM_TOKEN|PREE_ALLOWED_ORIGIN|STORAGE_MOUNT_PATH)["']*\s*[= ]/im]
];

const hits = [];
for (const [label, re] of RULES) if (re.test(text)) hits.push(label);

if (hits.length) {
  console.error(
    'BLOCKED by bluestaq-foundations secret-scan hook. The content matches: ' +
    hits.join(', ') + '.\n' +
    'No secret may be written to source: use an environment variable or a runtime ' +
    'bring-your-own-key input, and render the value as [REDACTED:type] in any file. ' +
    'A client-side access gate (a hardcoded PIN) is banned; see skills/security-hardening. ' +
    'A Dockerfile "ENV PORT=" line is banned; the app must read process.env.PORT and ' +
    'default to 8080; see skills/release-and-deploy and skills/app-store-deployment.'
  );
  process.exit(2); // block
}
process.exit(0); // allow
