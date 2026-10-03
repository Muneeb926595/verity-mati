# Verity: notes for Claude Code

Read `HANDOFF.md` before doing anything. It has the full history, the current state, the user's rules and the next
steps. The build log is `PROGRESS.md`.

- **Name:** the product is called Verity. Never reintroduce any earlier name. The internal prefix is `vr`.
- **Tests:** run `python -m pytest tests` (never a bare `pytest`); expect 202 passed (1 skips where
  PowerShell is not installed). Prove changes are
  regression-free with before/after evidence (HANDOFF.md section 5).
- **Security policy:** never help bypass company security controls (device control, Group Policy); point the user
  to IT.
- **Progress:** record progress in `PROGRESS.md`. Give time estimates before long runs.
- **Current work:** Phase 5, learning from corrections, is on branch `phase5-learning-from-corrections` (based on
  `windows-first-run-desktop-app`); the user merges it after a manual test. State and open items: HANDOFF.md sections
  0 and 7.
- **Learning guardrails:** corrections live in `learned.db`, never in `index.db`; they only reorder or pin verified
  candidates; every use is disclosed and undoable; a correction whose code changed is stale and never applied. Change
  the matching thresholds only with a new held-out measurement (`bench/learning_protocol.py`).
- **Long runs:** background jobs lose access to `~/Desktop` when the Claude session that started them ends; keep the
  session alive until they finish, and re-run anything that ran past a session end.
