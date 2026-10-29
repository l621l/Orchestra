# Base tool contract

## Runtime profiles and specifications

Operational settings live in `settings.json`; credentials stay in
`config.local.json`. Start the normal small-change workflow with
`python orchestra.py --workspace PATH`. For building a Python project use
`python orchestra.py --workspace PATH --profile project`. An alternative settings
file can be selected with `--settings PATH`. Paths supplied here are relative to
the invoking terminal; the default settings file is beside `orchestra.py`.

The `project` entry under `profiles` overrides base values: 60 shared model calls,
12 calls per worker, 30 minutes per turn, and a 120-second finalization reserve.
Worker editing in isolated worktrees is enabled, with up to two delegations. Reading/search stays available
after edits and during the closing reserve; no new workers start in that reserve.
The focused workflow retains its bounded research and focused-read limits.
These are initial experimental budgets, not a guarantee that all work will fit.

Tune call/time/token limits in `api`, `generation`, `execution`, and `roles`.
`implementation_read_max_lines` and `edit_retry_limit` control focused reads and
repeated edit failures. `verification_reserve_calls` triggers automatic checks.
`tool_limits` configures Git/check timeouts and local-tool concurrency. File/path
safety limits are still fixed tool contracts rather than runtime budget settings.
Selected effective settings are recorded in each turn's `runtime_configuration`
event. Profile overrides reject unknown keys; their `checks` section replaces the
base check list. `{python}` resolves to the interpreter running Orchestra.

The project profile runs `python_tests` using unittest discovery in `tests/`;
zero discovered tests are a failure. This checks the project's own tests and is
not an independent acceptance grade. The project profile enables autonomous local
commits and creation of new branches through its `git` settings.

Place the agreed specification in `whattodo.md` in the target workspace and say:
"Implement the complete specification in whattodo.md. Read it fully first,
maintain REQUIREMENTS.md, run the configured checks and report missing requirements."
The prompt requires following read continuations and preserves the specification.
Keep numbered requirements stable. The runtime budgets still apply per user turn,
not across the lifetime of the project.

Each agent's file tools operate inside its configured workspace. The director can
edit and run configured checks. Research workers can list, read and search;
implementation workers additionally edit/check in a private Git worktree. Tool schemas are
validated before dispatch; tool errors return `ok: false`, a stable error code,
an actionable message and optional details. No arbitrary command execution is
exposed to a model.

| Tool | Contract |
| --- | --- |
| `list_files` | Bounded tree; inspect `truncated` and `continuation_paths`. |
| `read_file` | UTF-8, inclusive 1-based ranges, whole-line continuation. Copy text without line-number prefixes when editing. |
| `search_text`, `search_regex` | Bounded results; cursors refer to saved snapshots. Start a new search for fresh results after edits. |
| `create_file` | New UTF-8 text file, at most 200,000 characters. Creates missing parents, publishes complete contents without overwriting. Works without Git. |
| `replace_text` | Existing UTF-8 file up to 2 MB; exactly one matching fragment, including overlapping matches. Preserves BOM and LF/CRLF. Ambiguous, stale or identical replacements change nothing. |
| `apply_patch` | Optional multi-file Git diff. Requires Git installed, but not a repository. Checks the whole diff before applying; rejects unsupported operations and restricted paths. Failure details include Git's diagnostic. |
| `git_status`, `git_diff`, `git_log`, `git_show` | Available at a Git repository root. Restricted paths are excluded. `git_diff` shows tracked changes; use the returned edit diff and `read_file` to inspect new files. |
| `git_create_branch` | Create and select a new local branch at current HEAD, including unborn repositories, when enabled. Refuses existing branches and pre-existing staged changes. Call alone; never force-switches or overwrites a branch. |
| `prepare_commit` | Read-only snapshot of permitted changed paths and bounded tracked diff. Fingerprint includes HEAD, branch and untracked contents. Read new files separately to review their contents. |
| `create_commit` | Requires the prepared fingerprint and a whole-file path group. Refuses any pre-existing staged changes, including restricted files. Call alone after edits/checks finish. Creates a local commit; never pushes. |
| `run_check` | Exact configured name; no model-supplied arguments. Available without Git. Output is bounded; timeout defaults to 120 seconds and is configurable in `tool_limits`; timed-out process trees are terminated where supported. |
| `delegate_task` | Optional `mode`: research (default) or implementation. Coding requires worker editing enabled and a Git HEAD. Workers cannot delegate. |
| `review_task_changes` | Director-only review of a finished worker patch; bounded diff, changed paths, fingerprint. Optional path for per-file review. |
| `apply_task_changes` | Director-only, must be called alone. Transfers a reviewed patch if affected director files match the worker baseline; conflicts reject the whole transfer. Invalidates director checks. |
| `report_blocker` | Director or implementation-worker control action. Must be called alone; preserves changes, ends tool use, requests a final `partial` report. |

Example editing sequence (one tool response at a time):

```json
{"tool":"create_file","arguments":{"path":"src/example.py","content":"value = 1\n"}}
{"tool":"replace_text","arguments":{"path":"src/example.py","old_text":"value = 1","new_text":"value = 2"}}
{"tool":"run_check","arguments":{"name":"python_compile"}}
```

The example is an explanation of function calls, not an additional wire format.
Actual calls use the provider's tool-call protocol.

Git stdin/stdout use explicit UTF-8 bytes, preserving LF/CRLF through the process
pipe instead of Windows text-mode newline conversion. Worker patch paths are
derived from the actual staged diff after Git normalization, excluding status
entries that no longer represent changes. Every editing agent receives configured
run_check names both as a schema enum and in resource status; schemas are copied
per agent rather than mutating shared tool definitions.

Each edit returns changed paths and a bounded diff. Path traversal, restricted
files/directories, paths escaping through links, Windows device names and
alternate data streams are rejected. All local edits share a write lock;
checks wait for that lock. A check and an edit cannot share one model response.
Two errors with the same code disable that editing tool for the turn. Use a
corrected alternative or report a blocker.

## Isolated implementation workers

### Comparison strategy

`--profile comparison` sets project workflow, autonomous Git, editing workers and
a director without direct file-edit tools. It also removes direct create_commit
and apply_task_changes; adoption must use the comparison transaction. Existing
default/project profiles retain direct strategy.

`start_comparison_round` must be called alone. It checks the shared budget and
director reserve, then prepares exactly two isolated workers under the director
write lock before either starts. They get identical tasks, equal allocations and
the same accepted HEAD. A clean committed director workspace/current branch is
required. Only one round is active. `execution.max_comparison_rounds` (default 3)
caps rounds per turn; shared call/time limits still apply. Coding delegation via
delegate_task is unavailable; separate research is allowed outside active rounds.

After both finish, both available patches must be reviewed. `promote_candidate`
selects one entire candidate, verifies the original director branch/HEAD and clean
state, applies the frozen patch, runs configured checks and commits the selected
paths. All checks must pass; no checks means no check gate. The new commit is the
baseline of the next pair. `reject_comparison_round` keeps the accepted baseline.
All round-control calls must be alone; no mixed write/check/delegation batch.

Failure after transfer triggers reversal of the owned patch. Reversal is refused
if candidate files or Git HEAD changed concurrently. Other check side effects are
preserved and disclosed; promoted=false/rolled_back=false requires inspection.
No user reset or forced branch movement is performed. Old variants are retained.
Pending unaccepted rounds prevent a completed final status. Events record starts,
decisions, rationales, accepted commits and executed promotion checks.

`roles.worker.allow_editing` is false by default and true in the project profile.
Research uses the director workspace read-only. Implementation creates a detached
worktree from HEAD, overlays permitted current dirty/staged files under the
director write lock, and records a private baseline without changing the director
branch/index. Dirty snapshots require configured Git identity for their internal
detached checkpoint. No repository or no initial commit yields an explicit setup
error; research and director editing still work without Git.

An implementation worker receives its own path policy, file tools, repository and
write lock; model clients, status display and budget accounting are shared. It may
edit, inspect Git, run configured checks and report blockers, but cannot commit,
manage branches, delegate or transfer patches. Requests retain the worker call cap.
Before completion, all available edits are collected, including on cancellation or
failure. File changes count as partial evidence even if no prose report survived.

The director reviews and explicitly accepts a frozen patch. Transfer compares
baseline hashes for every affected path; any mismatch rejects the entire patch.
Disjoint results can be applied sequentially; competing versions of a changed file
need explicit reconciliation. Normal apply_patch limits apply (text creation/edit,
30 files, 200000 characters; no deletions/renames/binary/mode changes). Worker checks
are evidence only: director verification is invalidated on transfer and must run
on the combined result. Only director edits/transfers enter root modified_files.

Worktrees and JSON patch artifacts stay under `logs/<date>/_workspaces/<run>/`,
with paths in worker result/events. Do not delete registered worktrees as ordinary
logs; use Git worktree removal after preserving results. Review/apply tools address
the current turn's workers; cross-session automatic resume is not implemented.
Working-directory isolation is not an OS sandbox for configured check programs.

## Recovery and verification

Implementation recovery is independent of the focused/project phase. After
length/timeout/connection failure, tool-capable recovery disables thinking until
a successful edit. `execution.max_implementation_recoveries` (default 1) bounds
recovery attempts between successful edits; repeated pre-edit prose also counts.
Exhaustion requests a partial final report without tools. If this final request
also fails or truncates, a runtime partial report preserves available draft text,
edits and verification state. A length-truncated tool call is discarded, never
dispatched. Recovery policy, progress and exhaustion are recorded in events.

Received answer/reasoning text on length or stream failure is archived in
`drafts/<request_id>.json` under the turn log. `request_draft_saved` records its
path and size, not the full text in events. Recovery and newly submitted workers
receive a bounded excerpt of the latest draft of their parent task, marked as
unverified evidence. `execution.draft_context_characters` limits excerpt text
(default 6000, 0 disables handoff, maximum 20000); wrappers add a small overhead.
Long drafts retain beginning/end with an omission marker, without a model call.
Already running workers are not updated. `request_draft_handoff` records worker
delivery. Drafts do not establish implementation or verification progress.

`summary.json` records modified files, actual tool results, edit/check/blocker
counts and `verification`:

- `not_configured`: no checks configured;
- `not_run`: checks exist but have not run;
- `passed`: at least one configured check passed on the current revision and
  none of the recorded checks for that revision failed;
- `failed`: a check failed or could not execute on the current revision;
- `stale`: recorded checks describe an earlier revision, before the latest edit.

For implementation requests, configured checks must be `passed` before the
runtime returns `completed`. Missing edits, unresolved editing errors or an
explicit blocker also produce `partial`. Passing a syntax check does not prove
functional correctness; choose relevant configured checks and report coverage
gaps. `completed` is not an independent acceptance grade for the task.

For implementation turns with pending edits and at most `verification_reserve_calls` model calls left (default two)
or a forced final response, the application runs the configured checks locally
before the next model request. These checks consume no API calls and their output
is included as evidence for the director and recorded in the trace. A subsequent
edit invalidates them and triggers another check before the final response.
Checks already recorded for the current revision are not repeated automatically.
This fallback does not run after an explicit blocker. Checks are bounded by the
remaining turn time, leaving `verification_report_reserve_seconds` (default 15)
for the report; skipped checks are disclosed rather than treated as passed.

Checks are administrator-configured programs and can have their own effects;
they are not a security sandbox. Their output is spooled to a temporary file
and bounded when returned to the model. Temporary edits publish atomically on
filesystems supporting hard links and atomic replacement. External processes
must not concurrently modify the workspace; the write lock coordinates only
this Orchestra process.

`git.commit_mode` accepts `manual`, `autonomous`, or `disabled` (base default:
manual). In manual mode, approval of the exact message/path group is a model
instruction; the runtime does not independently authenticate it. In autonomous
mode, no approval is requested, but the runtime requires every configured check
to have passed after the latest tool edit in the current turn; no configured
checks means no check gate. Failed/stale/missing checks block autonomous commits.
Disabled mode removes `create_commit` from the exposed and dispatched tools.
`git.allow_branch_creation` separately controls `git_create_branch` (base false).
Both autonomous commits and branch creation are enabled by the project profile.
The effective policy is given to the director every turn and printed at startup.
Workers receive neither Git mutation tool. Branch creation and commits must each
be the only call in their response and share the write lock with edits/checks.
Snapshot/index validation is enforced by code. Renames/copies are not supported for commit preparation.
If a commit succeeds but follow-up inspection fails, the tool returns
`committed: true` with warnings rather than inviting a duplicate commit.

## Offline acceptance

From the project directory, run:

```text
python -B -m unittest discover -v
```

The suite uses temporary workspaces, real tool dispatch, real Git operations,
small local check programs and real trace files. Model responses are scripted;
it makes no API requests. Coverage includes creation, reading/search,
replacement, multi-file patch refusal without partial writes, invalid
arguments and paths, duplicate/concurrent creation, check failure/truncation/
timeout, worker restrictions and blocker finalization. The symbolic-link test
skips when the OS does not permit the test user to create a link.


## Comparison acceptance

`start_comparison_round` takes short `acceptance_checks` strings and returns
stable, round-local IDs C1, C2, etc. The same catalog is included in the worker
assignment. Do not repeat criterion text during promotion.

Each `promote_candidate.acceptance` entry contains `id`, `evidence`, and exactly
one of these evidence sources:

- `check`: a configured check name, with actual test names/results in evidence.
- `files`: candidate-relative paths, with concrete review findings in evidence.
  Use this for README/coverage documentation; no dummy automated check is needed.

Example: `{"id":"C1","check":"python_tests","evidence":"test_create passed"}`.
Example: `{"id":"C2","files":["README.md"],"evidence":"Launch and error codes documented"}`.

Unknown/duplicate/missing IDs and unknown checks refuse promotion before
transfer. Errors return the criterion catalog, available checks and examples.
Review files must exist and pass the workspace path policy. This validates
evidence structure, not its semantic truth: the director still reviews test
coverage. All configured checks still rerun during promotion, even for file
review evidence. Empty alternatives need no diff review; nonempty ones do.

`decision: checkpoint` with empty acceptance and gaps in rationale closes the
round and returns the saved artifact/fingerprint without changing or committing
the accepted branch. Later rounds still start at the accepted commit; checkpoints
are not automatically resumed. Execution and acceptance statuses are separate.

If a specification defines stages, delegate one stage at a time. Explicit
skeleton stubs can satisfy the skeleton stage but cannot satisfy future behavior
requirements. A run may stop between stages; never merge them to fit a call limit.
