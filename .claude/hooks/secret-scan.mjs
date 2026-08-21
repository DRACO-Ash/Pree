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
  ['Dockerfile ENV PORT',        /^\s*ENV\s+PORT\s*=/im]
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
