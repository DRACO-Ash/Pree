#!/usr/bin/env node
// run-tests.mjs :: the test entry point for a quality-gated Bluestaq App Store container.
// Shipped as a scaffold template by deploy-recipes. Wire it as your "test" script:
//   "scripts": { "test": "node run-tests.mjs" }
//
// It OWNS the runner flags and tolerates the platform's exact invocation. The App Store
// runs `npm test -- --coverage`; node's runner rejects a bare `--coverage` with "bad
// option", so a naive `node --test` fails the platform test stage and skips every later
// gate. This entry ignores extra CLI arguments, scopes discovery to an explicit glob
// (never a bare directory, which misbehaves, and never auto-discovery, which would pick up
// a non-test helper basenamed like a test), and always emits coverage/lcov.info, the
// artefact the SonarQube gate reads.

import { spawnSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';

mkdirSync('coverage', { recursive: true });

// Extra CLI args (such as the platform's "--coverage") are intentionally ignored: this
// script decides the flags. Coverage is always on, so the report exists on every run.
const args = [
  '--test',
  '--experimental-test-coverage',
  '--test-reporter=lcov',
  '--test-reporter-destination=coverage/lcov.info',
  '--test-reporter=spec',
  '--test-reporter-destination=stdout',
  'test/*.test.js',            // explicit glob; adjust to your layout, never a bare directory
];

const r = spawnSync(process.execPath, args, { stdio: 'inherit' });
process.exit(r.status === null ? 1 : r.status);
