// eslint.config.mjs :: a Sonar-equivalent lint profile for a quality-gated App Store app.
// Shipped as a scaffold template by deploy-recipes and appstore-gate-compliance. It
// approximates the platform SonarQube ruleset LOCALLY, so violations are fixed one at a
// time from the first commit rather than six hundred at once on the first upload. Wire it
// into the loop (an "npm run lint" step) and keep the count at zero.
//
// Add the dev dependencies, pinned to exact versions:
//   npm i -D -E eslint eslint-plugin-unicorn eslint-plugin-sonarjs
//
// The two plugin sets together approximate Sonar's defaults (cognitive complexity, modern
// API preferences, loop shapes, comparators). They will not be byte-identical to the
// server-side gate, but they collapse the first-upload violation wave to near zero.

import unicorn from 'eslint-plugin-unicorn';
import sonarjs from 'eslint-plugin-sonarjs';

export default [
  { ignores: ['coverage/**', 'dist/**', 'node_modules/**'] },
  unicorn.configs['flat/recommended'],
  sonarjs.configs.recommended,
  {
    rules: {
      // Sonar's cognitive-complexity ceiling. Resolve a hit by extracting named helpers
      // with no behaviour change, not by suppressing the rule.
      'sonarjs/cognitive-complexity': ['error', 15],
    },
  },
];
