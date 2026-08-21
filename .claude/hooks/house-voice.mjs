#!/usr/bin/env node
// house-voice.mjs :: PreToolUse guardrail for Write|Edit|MultiEdit and Bash.
// Enforces the non-negotiable house-voice rules at the mechanism, not just in prose,
// because a style rule followed only from memory drifts under load across a long session
// (a real finding: em-dashes reached several commit subjects before a reviewer caught them).
//
// Scope is deliberately the UNAMBIGUOUS rules only, so the hook never cries wolf:
//   - The em-dash (U+2014) is banned everywhere in this house, so it is blocked in any
//     authored content and in any git commit message.
//   - "+" meaning "and" (a word, a space, "+", a space, a word) is blocked in commit
//     messages, where "+" almost never means addition.
// US spellings are NOT enforced here on purpose: a blocking grep for "color", "center",
// or "behavior" would fire on every CSS property and DOM API, so US spelling stays owned
// by the reviewers and the house-voice output style. A hook that false-positives gets
// switched off, which is worse than no hook.
//
// Claude Code passes a JSON payload on stdin with tool_name and tool_input.
// Exit 0 = allow. Exit 2 = block; stderr is shown to the model as the reason.

import { readFileSync } from 'node:fs';

let raw = '';
try { raw = readFileSync(0, 'utf8'); } catch { process.exit(0); }

let payload = {};
try { payload = JSON.parse(raw || '{}'); } catch { process.exit(0); }

const tool = payload.tool_name || '';
const ti = payload.tool_input || {};

const EM_DASH = /\u2014/;                        // the long em-dash, the one dash to avoid
const PLUS_FOR_AND = /[A-Za-z]{2,} \+ [A-Za-z]{2,}/;  // "auth + config" style, commit messages only

const hits = [];

if (tool === 'Write' || tool === 'Edit' || tool === 'MultiEdit') {
  // Only the NEW content the model is writing, so an em-dash already on disk is not our concern.
  const parts = [ti.content, ti.new_string, ti.file_text];
  if (Array.isArray(ti.edits)) for (const e of ti.edits) parts.push(e && e.new_string);
  const text = parts.filter(s => typeof s === 'string').join('\n');
  if (EM_DASH.test(text)) hits.push('an em-dash (U+2014) in the authored content');
} else if (tool === 'Bash') {
  const cmd = typeof ti.command === 'string' ? ti.command : '';
  // Only inspect commit messages; other commands legitimately carry "--" flags and maths.
  if (/\bgit\s+commit\b/.test(cmd)) {
    if (EM_DASH.test(cmd)) hits.push('an em-dash (U+2014) in the commit message');
    if (PLUS_FOR_AND.test(cmd)) hits.push('a "+" used to mean "and" in the commit message');
  }
}

if (hits.length) {
  console.error(
    'Held by the bluestaq-foundations house-voice hook. The content contains: ' +
    hits.join(', ') + '.\n' +
    'The house voice is a guide, not a leash, but two things it does hold to: avoid the ' +
    'long em-dash (a single dash or a reworded sentence reads better), and do not use "+" ' +
    'to mean "and" in prose (write "and"). Everything else, including UK spelling and ' +
    'typography, is guidance, not a gate. Fix these two and retry.'
  );
  process.exit(2); // block
}
process.exit(0); // allow
