# Working on Orchestra

- Read the current repository files before editing. Historical working copies
  are reference material, not authoritative sources for whole-file replacement.
- Preserve uncommitted changes. When restoring an older fix, inspect both sides
  of the diff and merge the specific behavior with newer work.
- If edits must be prepared outside this directory, snapshot the current inputs.
  Before transfer, compare every original input against the current repository;
  stop and re-merge if anything changed. Transfer only reviewed changed files.
- After changes to tools, dispatch, execution phases or verification, run the full
  offline suite from this repository: `python -B -m unittest discover -v`.
  Run it again in the actual repository after transferring external edits.
- Report actual passing/failing/skipped counts. A single test module is not the
  full suite. Keep existing regression tests; do not weaken them to hide regressions.
- API smoke tests spend money; offline tests use scripted model responses.
