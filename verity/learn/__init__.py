"""Learning from corrections (Phase 5, docs/development/VERITY_LEARNING_FROM_CORRECTIONS_ONE_PAGER.html).

A person who knows the code says once where the right answer is; every later answer on that project uses it, openly
and reversibly, and without inventing anything:

- store.py     learned.db, next to the index but separate from it (the index can be rebuilt, corrections cannot)
- anchor.py    after every index, a correction follows its line or turns stale when the code changes
- apply.py     per question: pin (same question), hint (similar question), accepted glossary words, disclosure
- service.py   the operations every client uses (engine RPC, CLI, MCP): correct, confirm, list, undo, export, import

A correction only reorders the candidates the analysis found, or builds one from real code at the corrected line.
Every claim still goes through the verifier, and nothing leaves the machine."""
