---
name: handover
description: Write a one-page handover so the next session resumes without lost context. Use when pausing, switching goal, before a long run, or when the session has run for hours.
---
# Handover

Write `docs/HANDOVER.md` (overwrite), under one page, with these headings:

1. **State**: current version, branch, last passing commit SHA, gate status per stage with the command that proved it.
2. **Done this session**: changes with commit SHAs.
3. **Decisions and sign-offs**: what the user decided and when.
4. **Ledger**: fixed and verified; flagged but not fixed; assumptions made.
5. **Open risks**.
6. **Next action**: one line, the first thing the next session does.

Commit the file. The SessionStart hook surfaces the "Next action" line automatically.
