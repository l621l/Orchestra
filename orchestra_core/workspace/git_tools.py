"""Workspace editing, configured checks and restricted Git inspection."""
import os
import hashlib
import tempfile
import stat
import re
import signal
import difflib
from orchestra_core.workspace.tool_errors import ToolError
from orchestra_core.workspace.project_map import HEADER, ProjectMap
from orchestra_core.workspace.interface_notes import FILE as NOTES_FILE, InterfaceNotes, atomic_text
import subprocess
from pathlib import Path, PurePosixPath

from orchestra_core.workspace.tools import definition
from orchestra_core.workspace.workspace_policy import WorkspacePolicy
from orchestra_core.workspace.tool_contracts import EDIT_TOOL_NAMES, validate_arguments


GIT_TOOL_NAMES = {"git_status", "git_diff", "git_log", "git_show",
                  "git_create_branch", "git_pull", "git_push", "run_check", "prepare_commit", "create_commit", *EDIT_TOOL_NAMES}
WORKSPACE_TOOL_NAMES = {"run_check", *EDIT_TOOL_NAMES}

GIT_TOOLS = [
    definition("git_push", "Publish commits from the current branch to its upstream, or to the same branch on origin on the first push. Call alone when the user requests publication.", {}, []),
    definition("git_pull", "Update the current branch from its configured upstream with fast-forward only. Call alone with a clean working tree.", {}, []),
    definition("create_file", "Create a new UTF-8 text file, including missing parent directories. Fails if the path already exists; never overwrites. Use replace_text for an existing file. Does not require Git or stage changes.",
               {"path": {"type": "string", "description": "New workspace-relative file path."},
                "content": {"type": "string", "maxLength": 200000,
                            "description": "Complete file contents, up to 200000 characters; empty is allowed."}},
               ["path", "content"]),
    definition("replace_text", "Preferred for editing an existing UTF-8 file. Replace exactly one occurrence of old_text with new_text; include enough unchanged context to make old_text unique. Missing or ambiguous matches leave the file unchanged. Preserves LF/CRLF and does not stage or commit.",
               {"path": {"type": "string", "description": "Workspace-relative existing file."},
                "old_text": {"type": "string", "description": "Exact non-empty unique fragment from the current file, without line-number prefixes."},
                "new_text": {"type": "string", "description": "Replacement fragment; may be empty to remove the matched text."},
                "expected_sha256": {"type": "string", "minLength": 64, "maxLength": 64,
                    "description": "Optional content_sha256 from read_file. Reject if the file changed since that read."}},
               ["path", "old_text", "new_text"]),
    definition("git_status", "Show the current branch and compact working-tree status, including untracked files.",
               {}, []),
    definition("git_diff", "Show an unstaged or staged Git diff, optionally restricted to one relative file or directory.",
               {"staged": {"type": "boolean", "description": "Show staged changes; default false."},
                "path": {"type": "string", "description": "Optional relative file or directory."},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 20,
                                  "description": "Unchanged context lines; default 3."}}, []),
    definition("git_log", "Show recent commits, optionally filtered by a relative file or directory.",
               {"max_count": {"type": "integer", "minimum": 1, "maximum": 50,
                              "description": "Number of commits; default 10."},
                "path": {"type": "string", "description": "Optional relative file or directory."}}, []),
    definition("git_show", "Use after git_log to inspect one historical commit. Returns bounded commit metadata and a patch for permitted files, optionally filtered by a relative path. Check truncated before treating the output as complete. Accepts a SHA, branch, tag, HEAD, or relative revision such as HEAD~2; arbitrary Git options are rejected. Read-only.",
               {"revision": {"type": "string", "description": "Commit, branch, tag, HEAD, or relative revision such as HEAD~2."},
                "path": {"type": "string", "description": "Optional relative file or directory."},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 20,
                                  "description": "Unchanged context lines; default 3."}}, ["revision"]),
    definition("run_check", "Run one configured verification command by exact name, before editing for a baseline or after editing to verify the result. Runs in listed order; place it after edits to verify their result. Returns passed, returncode and bounded output. Failure does not revert files.",
               {"name": {"type": "string", "description": "Exact configured check name, for example python_compile."}}, ["name"]),
    definition("git_create_branch", "Create and switch to a NEW local branch from the current HEAD, including before the first commit. Call alone before implementation when branch creation is enabled by runtime policy. Preserves working files; refuses an existing branch or pre-existing staged changes. Cannot switch to an existing branch, reset history, merge, or push.",
               {"name": {"type": "string", "minLength": 1, "maxLength": 120,
                         "description": "New local branch name, for example feat/task-ledger."}}, ["name"]),
    definition("prepare_commit", "Inspect changed paths and get a fingerprint. Optional paths and message identify the verified group to commit and push if the user requested publication and the spending limit stops the run.", {
        "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "message": {"type": "string", "minLength": 1, "maxLength": 2000},
    }, []),
    definition("create_commit", "Create one local commit from the latest prepare_commit fingerprint and selected whole-file paths. Available only when Git mutations are enabled; requires successful configured checks after the latest edit. Refuses stale snapshots, pre-existing staged changes, or unprepared paths. Call alone after edits and checks. Never pushes or merges.",
               {"message": {"type": "string", "description": "Concise commit subject and optional body describing the completed change."},
                "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                          "description": "Related whole-file paths from prepare_commit, selected according to runtime policy."},
                "fingerprint": {"type": "string", "description": "Snapshot fingerprint from prepare_commit."}},
               ["message", "paths", "fingerprint"]),
    definition("apply_patch", "Apply a Git-style text diff with diff --git, ---, +++, and hunk headers. Use workspace-relative a/ and b/ paths. Supports creation and modification; rejects deletion, renames, copies, mode changes, and binary patches. Does not stage or commit. Works without a Git repository, but requires Git installed.",
               {"patch": {"type": "string", "description": "Git-style unified diff; each file starts with diff --git a/path b/path."}},
               ["patch"]),
]

# Semantic notes travel in the same edit call; no extra model round trip.
INTERFACE_NOTES_SCHEMA = {
    'type':'array','maxItems':8,'description':
    'Optional short semantic notes about changed public interfaces. Stored only after a successful edit, with source hashes. Do not describe local variables.',
    'items':{'type':'object','additionalProperties':False,'required':['path','symbol','note'],
        'properties':{'path':{'type':'string','minLength':1,'maxLength':240},
                      'symbol':{'type':'string','minLength':1,'maxLength':120},
                      'note':{'type':'string','minLength':1,'maxLength':700},
                      'related_paths':{'type':'array','maxItems':8,
                                       'items':{'type':'string','minLength':1,'maxLength':240}}}}}
for _tool in GIT_TOOLS:
    if _tool['function']['name'] in EDIT_TOOL_NAMES:
        _tool['function']['parameters']['properties']['interface_notes'] = INTERFACE_NOTES_SCHEMA

REVISION_PATTERN = re.compile(r"^[0-9A-Za-z@][0-9A-Za-z._/@^~-]*$")


class RepositoryTools:
    MAX_OUTPUT = 40_000
    MAX_PATCH = 200_000
    MAX_FILES_PER_PATCH = 30
    CHECK_TIMEOUT = 120
    GIT_TIMEOUT = 20

    def __init__(self, root, policy=None, checks=None, git_enabled=True):
        self.policy = policy or WorkspacePolicy(root)
        self.root = self.policy.root
        self.checks = checks or {}
        self.interface_map = ProjectMap(self.policy)
        self.interface_memory_enabled = True
        self.git_enabled = git_enabled
        self.available = False
        if not git_enabled:
            return
        try:
            probe = self._run(["rev-parse", "--show-toplevel"], check=False)
            discovered = probe["stdout"].strip()
            self.available = (probe["returncode"] == 0 and bool(discovered)
                              and Path(discovered).resolve(strict=True) == self.root)
        except (RuntimeError, ToolError):
            self.available = False

    def _require_repository(self):
        if not self.available:
            raise ToolError("not_a_repository", "Workspace is not the root of a Git repository.")

    def _run(self, arguments, input_text=None, check=True):
        if not self.git_enabled:
            raise ToolError('git_disabled', 'Git tools are disabled.')
        environment = os.environ.copy()
        environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat",
                            "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
        try:
            result = subprocess.run(
                ["git", "--literal-pathspecs", "-C", str(self.root), *arguments],
                # Text-mode stdin silently expands LF to CRLF on Windows. Git
                # patches must retain their exact bytes in both directions.
                input=input_text.encode('utf-8') if input_text is not None else None,
                capture_output=True, timeout=self.GIT_TIMEOUT, env=environment, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ToolError("git_unavailable", "Git could not run or timed out; check installation and execution limits.") from error
        if check and result.returncode != 0:
            raise ToolError("git_failed", "Git rejected the operation; check the repository state and arguments.")
        return {"returncode": result.returncode,
                "stdout": result.stdout.decode('utf-8', errors='replace'),
                "stderr": result.stderr.decode('utf-8', errors='replace')}

    @staticmethod
    def _bounded(text, limit=MAX_OUTPUT):
        if len(text) <= limit:
            return text, False
        return text[:limit - 1] + "…", True

    def _edit_diff(self, path, old, new, created=False):
        diff = ''.join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile='/dev/null' if created else f'a/{path}', tofile=f'b/{path}'))
        return self._bounded(diff)

    def _edit_result(self, path, old, new, **details):
        warnings = details.pop('warnings', [])
        try:
            diff, truncated = self._edit_diff(path, old, new, created=details.get('created', False))
        except Exception:
            # The write already succeeded; diagnostic failure must not invite a retry.
            diff, truncated = None, False
            warnings.append('File changed; diff could not be generated. Inspect it with read_file.')
        return {'applied': True, 'files': [path], 'files_changed': 1,
                'diff': diff, 'diff_truncated': truncated, 'warnings': warnings, **details}

    def _relative_path(self, value):
        relative = self.policy.relative(value)
        self.policy.resolve_write(value)
        return relative.as_posix()

    def _changed_paths(self, staged):
        arguments = ["diff", "--no-renames", "--name-only", "-z"]
        if staged:
            arguments.append("--cached")
        output = self._run(arguments)["stdout"]
        return [path for path in output.split("\0") if path]

    def _current_branch(self):
        return self._run(['symbolic-ref', '--quiet', '--short', 'HEAD'], check=False)['stdout'].strip()

    def _push_refs(self, expected_head=None):
        self._require_repository()
        branch = self._current_branch()
        if not branch:
            raise ToolError('detached_head', 'Select a local branch before pushing.')
        if expected_head is not None and self._run(
                ['rev-parse', '--verify', f'refs/heads/{branch}'])['stdout'].strip() != expected_head:
            raise ToolError('stale_commit_plan', 'The current branch tip changed before push.')
        remote = self._run(['config', '--get', f'branch.{branch}.remote'], check=False)['stdout'].strip()
        target = self._run(['config', '--get', f'branch.{branch}.merge'], check=False)['stdout'].strip()
        if bool(remote) != bool(target) or remote == '.' or (target and not target.startswith('refs/heads/')):
            raise ToolError('invalid_upstream', 'Configure a remote branch as the upstream before pushing.')
        destination = target or f'refs/heads/{branch}'
        source = f'refs/heads/{branch}'
        return branch, remote or 'origin', source, destination

    @staticmethod
    def _check_push_result(result):
        if result['returncode']:
            detail = result['stderr'].lower()
            if any(marker in detail for marker in (
                'could not read username', 'could not read password', 'terminal prompts disabled',
                'authentication failed', 'invalid username or token', 'permission denied (publickey)',
                'write access to repository not granted', 'returned error: 403',
            )):
                raise ToolError('push_auth_required',
                                'Git could not authenticate or the account lacks write access. '
                                'Sign in to the remote in this environment and try again.')
            if any(marker in detail for marker in ('non-fast-forward', 'fetch first')):
                raise ToolError('push_non_fast_forward',
                                'The remote branch has changes. Sync the branch before pushing.')
            raise ToolError('push_failed', 'Git rejected the push. Check the remote, access, and branch history.')

    def check_push(self):
        _, remote, source, destination = self._push_refs()
        if self._run(['remote', 'get-url', '--push', '--', remote], check=False)['returncode']:
            raise ToolError('push_remote_missing', 'Set the project’s push remote before starting.')
        if self._run(['rev-parse', '--verify', source], check=False)['returncode']:
            # An unborn branch has no source ref for a dry-run push.
            return
        result = self._run(['push', '--dry-run', '--no-verify', '--no-follow-tags', '--',
                            remote, f'{source}:{destination}'], check=False)
        self._check_push_result(result)

    def git_push(self, *, expected_head=None):
        branch, remote, source, destination = self._push_refs(expected_head)
        result = self._run(['push', '--no-follow-tags', '--set-upstream', '--',
                            remote, f'{source}:{destination}'], check=False)
        self._check_push_result(result)
        return {'pushed': True, 'branch': branch, 'remote': remote, 'destination': destination}

    def git_pull(self):
        self._require_repository()
        if not self._current_branch():
            raise ToolError('detached_head', 'Select a local branch before pulling.')
        upstream = self._run(['rev-parse', '--abbrev-ref', '@{upstream}'], check=False)
        if upstream['returncode']:
            raise ToolError('missing_upstream', 'The current branch has no configured upstream.')
        if self._run(['status', '--porcelain', '--untracked-files=all'])['stdout']:
            raise ToolError('dirty_worktree', 'Save local changes before pulling.')
        before = self._run(['rev-parse', 'HEAD'])['stdout'].strip()
        result = self._run(['-c', 'rebase.autoStash=false', '-c', 'merge.autoStash=false',
                            'pull', '--ff-only', '--no-rebase'], check=False)
        if result['returncode']:
            raise ToolError('pull_failed', 'Fast-forward pull failed. Check remote access and whether the branch diverged.')
        head = self._run(['rev-parse', 'HEAD'])['stdout'].strip()
        return {'upstream': upstream['stdout'].strip(), 'head': head, 'updated': head != before}

    def git_status(self):
        self._require_repository()
        branch = self._current_branch()
        raw = self._run(["status", "--porcelain=v1", "-z",
                         "--untracked-files=all"])["stdout"]
        tokens = raw.split("\0")
        lines = [f"## {branch or '(detached HEAD)'}"]
        index = 0
        while index < len(tokens):
            entry = tokens[index]
            index += 1
            if not entry:
                continue
            code = entry[:2]
            current = entry[3:]
            paths = [current]
            original = None
            if ("R" in code or "C" in code) and index < len(tokens):
                original = tokens[index]
                index += 1
                paths.append(original)
            if not all(self.policy.allows_relative(path, write=True)
                       for path in paths):
                continue
            shown = f"{original} -> {current}" if original is not None else current
            lines.append(f"{code} {shown}")
        output, truncated = self._bounded("\n".join(lines) + "\n")
        return {"status": output, "truncated": truncated}

    def git_diff(self, staged=False, path=None, context_lines=3):
        self._require_repository()
        if type(staged) is not bool:
            raise ToolError("invalid_arguments", "staged must be a boolean.")
        if type(context_lines) is not int or not 0 <= context_lines <= 20:
            raise ToolError("invalid_arguments", "context_lines must be an integer from 0 to 20.")
        arguments = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color",
                     f"--unified={context_lines}"]
        if staged:
            arguments.append("--cached")
        safe_path = self._relative_path(path) if path is not None else None
        allowed_paths = self._filter_paths(self._changed_paths(staged), safe_path)
        if not allowed_paths:
            return {"diff": "", "staged": staged, "path": safe_path,
                    "truncated": False}
        arguments.extend(["--", *allowed_paths])
        result = self._run(arguments)
        output, truncated = self._bounded(result["stdout"])
        return {"diff": output, "staged": staged, "path": safe_path,
                "truncated": truncated}

    def git_log(self, max_count=10, path=None):
        self._require_repository()
        if type(max_count) is not int or not 1 <= max_count <= 50:
            raise ToolError("invalid_arguments", "max_count must be an integer from 1 to 50.")
        if self._run(['rev-parse', '--verify', 'HEAD'], check=False)['returncode']:
            if path is not None:
                self._relative_path(path)
            return {'log': '', 'commits': 0, 'path': path, 'truncated': False,
                    'message': 'Repository has no commits yet.'}
        arguments = ["log", "--no-decorate", "--date=iso-strict",
                     "--pretty=format:%H%x09%ad%x09%an%x09%s",
                     f"--max-count={max_count}"]
        safe_path = self._relative_path(path) if path is not None else None
        if safe_path:
            arguments.extend(["--", safe_path])
        result = self._run(arguments)
        output, truncated = self._bounded(result["stdout"])
        commits = output.count("\n") + bool(output)
        return {"log": output, "commits": commits, "path": safe_path,
                "truncated": truncated}

    def _filter_paths(self, paths, requested=None):
        prefix = "" if requested in (None, ".") else requested.rstrip("/") + "/"
        return list(dict.fromkeys(candidate for candidate in paths
                    if candidate and (candidate == requested or candidate.startswith(prefix))
                    and self.policy.allows_relative(candidate, write=True)))

    def git_show(self, revision, path=None, context_lines=3):
        self._require_repository()
        if not isinstance(revision, str) or not REVISION_PATTERN.fullmatch(revision):
            raise ToolError("invalid_arguments", "revision contains unsupported characters.")
        if type(context_lines) is not int or not 0 <= context_lines <= 20:
            raise ToolError("invalid_arguments", "context_lines must be an integer from 0 to 20.")
        resolved = self._run(["rev-parse", "--verify", f"{revision}^{{commit}}"]) ["stdout"].strip()
        safe_path = self._relative_path(path) if path is not None else None
        changed = self._run(["diff-tree", "--root", "-m", "--no-commit-id",
                             "--no-renames", "--name-only", "-r", "-z", resolved])["stdout"]
        allowed = self._filter_paths(changed.split("\0"), safe_path)
        metadata = self._run(["show", "--no-patch", "--no-color", "--date=iso-strict",
                              "--format=fuller", resolved])["stdout"]
        patch = ""
        if allowed:
            patch = self._run(["show", "-m", "--no-ext-diff", "--no-textconv",
                               "--no-renames", "--no-color", "--format=",
                               f"--unified={context_lines}", resolved, "--", *allowed])["stdout"]
        output, truncated = self._bounded(metadata + patch)
        return {"show": output, "revision": revision, "commit": resolved,
                "path": safe_path, "truncated": truncated}

    def _check_output(self, captured):
        """Bound memory/output while retaining both setup and final traceback."""
        captured.seek(0, os.SEEK_END)
        size = captured.tell()
        # Byte budget also bounds decoded characters, including malformed UTF-8.
        cap = self.MAX_OUTPUT - 100
        captured.seek(0)
        if size <= cap:
            return {'output': captured.read(cap).decode('utf-8', errors='replace'),
                    'truncated': False, 'omitted_bytes': 0}
        head_size = cap // 4
        tail_size = cap - head_size
        head = captured.read(head_size).decode('utf-8', errors='replace')
        captured.seek(-tail_size, os.SEEK_END)
        tail = captured.read(tail_size).decode('utf-8', errors='replace')
        omitted = size - cap
        return {'output': head + f'\n... [{omitted} bytes omitted] ...\n' + tail,
                'truncated': True, 'omitted_bytes': omitted}

    def run_check(self, name, *, timeout_seconds=None):
        if not isinstance(name, str) or name not in self.checks:
            available = ", ".join(sorted(self.checks)) or "none"
            raise ToolError("invalid_arguments", f"Unknown check. Configured checks: {available}.")
        command = self.checks[name]
        timeout = self.CHECK_TIMEOUT if timeout_seconds is None else min(self.CHECK_TIMEOUT, timeout_seconds)
        # Spool output to disk: a noisy checker must not fill the agent's memory.
        with tempfile.TemporaryFile() as captured:
            try:
                process = subprocess.Popen(command, cwd=self.root, stdin=subprocess.DEVNULL,
                    stdout=captured, stderr=subprocess.STDOUT,
                    start_new_session=os.name != 'nt',
                    creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                                   if os.name == 'nt' else 0))
            except OSError as error:
                raise ToolError('check_unavailable', 'Configured check could not be started. Check its executable and working directory.') from error
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                try:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=10, check=False)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                raise ToolError('check_timeout', f'Check exceeded {timeout:g} seconds. Reduce its scope or fix the check before retrying.', **self._check_output(captured)) from error
            diagnostic = self._check_output(captured)
        return {'name': name, 'returncode': process.returncode,
                'passed': process.returncode == 0, **diagnostic}

    def _managed_commit_files(self):
        managed = []
        for path in (NOTES_FILE,):
            if not self.policy.allows_relative(path, write=True):
                continue
            target = self.policy.resolve_write(path)
            if not target.is_file() or (self.policy.root / path).is_symlink():
                continue
            try:
                InterfaceNotes(self.policy).load()
            except (ValueError, OSError):
                continue
            managed.append(path)
        return managed

    def _commit_snapshot(self):
        self._require_repository()
        head = self._run(["rev-parse", "--verify", "HEAD"], check=False)
        head_id = head["stdout"].strip() if head["returncode"] == 0 else None
        branch = self._run(["symbolic-ref", "--quiet", "HEAD"], check=False)["stdout"].strip()
        raw = self._run(["status", "--porcelain=v1", "-z",
                         "--untracked-files=all"])["stdout"]
        tokens = raw.split("\0")
        entries, paths, untracked = [], [], set()
        staged_present = False
        index = 0
        while index < len(tokens):
            entry = tokens[index]
            index += 1
            if not entry:
                continue
            code, current = entry[:2], entry[3:]
            # Check the entire index, including files that the model cannot see.
            # git commit would otherwise include those staged paths too.
            staged_present = staged_present or code[0] not in {" ", "?"}
            if "R" in code or "C" in code:
                raise ToolError("unsupported_commit_change",
                                "Automated commit planning does not yet support renamed or copied paths.")
            if not self.policy.allows_relative(current, write=True):
                continue
            safe_path = self._relative_path(current)
            entries.append(f"{code} {safe_path}")
            paths.append(safe_path)
            if code == "??":
                untracked.add(safe_path)
        managed = self._managed_commit_files()
        if managed:
            ignored = self._run(["ls-files", "--others", "--ignored", "--exclude-standard",
                                 "-z", "--", *managed])["stdout"].split("\0")
            for path in ignored:
                if path in managed and path not in paths:
                    entries.append(f"?? {path}")
                    paths.append(path)
                    untracked.add(path)
        paths = list(dict.fromkeys(paths))
        managed_paths = [path for path in managed if path in paths]
        visible_paths = [path for path in paths if path not in managed_paths]
        if not paths:
            raise ToolError("nothing_to_commit", "No accessible workspace changes are available to commit.")
        base = ["HEAD"] if head_id is not None else ["--cached"]
        full_diff = self._run(["diff", *base, "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color",
                               "--binary", "--", *visible_paths])["stdout"] if visible_paths else ""
        digest = hashlib.sha256()
        digest.update((str(head_id) + "\0" + branch + "\0").encode("utf-8"))
        digest.update("\n".join(entries).encode("utf-8") + b"\0")
        digest.update(full_diff.encode("utf-8"))
        for path in sorted(untracked | set(managed_paths)):
            target = self.policy.resolve_write(path)
            digest.update(b"\0" + path.encode("utf-8") + b"\0")
            if target.is_file():
                digest.update(target.read_bytes())
        shown_diff, truncated = self._bounded(full_diff)
        return {"fingerprint": digest.hexdigest(), "paths": visible_paths,
                "managed_paths": managed_paths,
                "head": head_id,
                "status": "\n".join(entry for entry in entries if entry[3:] not in managed_paths) + "\n",
                "staged_changes_present": staged_present,
                "diff": shown_diff, "diff_truncated": truncated}

    def prepare_commit(self, paths=None, message=None):
        snapshot = self._commit_snapshot()
        snapshot.pop("managed_paths")
        return snapshot

    def git_create_branch(self, name):
        self._require_repository()
        if (not isinstance(name, str) or not 1 <= len(name) <= 120
                or name.startswith('-') or name == 'HEAD'
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', name)):
            raise ToolError('invalid_arguments', 'Use a new branch name such as feat/task-ledger (letters, digits, dot, underscore, slash, hyphen; at most 120 characters).')
        if self._run(['check-ref-format', '--branch', name], check=False)['returncode']:
            raise ToolError('invalid_arguments', 'Invalid Git branch name. Use a name such as feat/task-ledger.')
        if self._run(['show-ref', '--verify', '--quiet', f'refs/heads/{name}'], check=False)['returncode'] == 0:
            raise ToolError('branch_exists', 'Branch already exists; choose a new name. Switching to an existing branch is not supported.')
        if self._changed_paths(staged=True):
            raise ToolError('staged_changes_present', 'Resolve pre-existing staged changes before creating a branch.')
        previous = self._run(['symbolic-ref', '--quiet', '--short', 'HEAD'], check=False)['stdout'].strip() or None
        self._run(['checkout', '-b', name])
        # Do not turn successful mutation into an error if optional inspection fails.
        status, warnings = None, []
        try:
            status = self.git_status()['status']
        except (ToolError, OSError):
            warnings.append('Branch created and selected; inspect git_status before continuing.')
        return {'created': True, 'branch': name, 'previous_branch': previous,
                'status': status, 'warnings': warnings}

    def _map_for_index(self):
        entries = self._run(['ls-files', '--stage', '-z'])['stdout'].split('\0')
        paths = []
        for entry in filter(None, entries):
            metadata, name = entry.split('\t', 1)
            if (metadata.split()[0] in {'100644', '100755'}
                    and name.endswith('.py') and self.policy.allows_relative(name, write=False)):
                paths.append(name)
        paths.sort(key=lambda name: (name.startswith('tests/') or Path(name).name.startswith('test_'), name))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            if paths:
                selected = paths[:self.interface_map.max_files + 1]
                self._run(['checkout-index', '--prefix=' + root.as_posix() + '/', '-z', '--stdin'],
                          input_text='\0'.join(selected) + '\0')
            ProjectMap(WorkspacePolicy(root), max_files=self.interface_map.max_files).refresh()
            return (root / 'PROJECT_MAP.md').read_text(encoding='utf-8')

    def create_commit(self, message, paths, fingerprint):
        self._require_repository()
        if (not isinstance(message, str) or not message.strip() or len(message) > 2000
                or len(message.splitlines()[0]) > 100):
            raise ToolError("invalid_arguments",
                            "message must be non-empty, at most 2000 characters, with a subject at most 100 characters.")
        if (not isinstance(paths, list) or not paths
                or any(not isinstance(path, str) for path in paths)):
            raise ToolError("invalid_arguments", "paths must be a non-empty string array.")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ToolError("invalid_arguments", "fingerprint must come from prepare_commit.")
        snapshot = self._commit_snapshot()
        if snapshot["fingerprint"] != fingerprint:
            raise ToolError("stale_commit_plan",
                            "Workspace changed after preparation; run prepare_commit again.")
        if snapshot["staged_changes_present"]:
            raise ToolError("staged_changes_present",
                            "Resolve pre-existing staged changes before automated commits.")
        safe_paths = list(dict.fromkeys(self._relative_path(path) for path in paths))
        managed = snapshot["managed_paths"]
        if not set(safe_paths).issubset([*snapshot["paths"], *managed]):
            raise ToolError("invalid_commit_paths",
                            "Every commit path must belong to the prepared snapshot.")
        ordinary = [path for path in safe_paths if path not in managed]
        safe_paths = ordinary + managed
        for identity in ('GIT_AUTHOR_IDENT', 'GIT_COMMITTER_IDENT'):
            if self._run(['var', identity], check=False)['returncode']:
                raise ToolError('git_identity_missing', 'Git author/committer identity is unavailable. Ask the user to configure user.name and user.email; no files were staged.')
        map_target = None
        previous_map = None
        map_written = False
        try:
            if ordinary:
                self._run(["add", "-A", "--", *ordinary])
            if managed:
                self._run(["add", "-f", "--", *managed])
            staged = set(self._changed_paths(staged=True))
            if not staged:
                raise ToolError("nothing_to_commit", "Selected paths contain no committable changes.")
            if staged != set(safe_paths):
                raise ToolError("index_changed", "Staged paths differ from the prepared group. Inspect the index and prepare again.")
            if self.interface_memory_enabled and self.interface_map.storage_target is not None:
                if not (self.root / 'PROJECT_MAP.md').is_symlink():
                    target = self.policy.resolve_write('PROJECT_MAP.md')
                    previous_map = target.read_bytes() if target.exists() else None
                    if previous_map is None or previous_map.startswith(HEADER.encode('utf-8')):
                        map_target = target
                        content = self._map_for_index()
                        if previous_map != content.encode('utf-8'):
                            atomic_text(target, content)
                            map_written = True
                        self._run(['add', '-f', '--', 'PROJECT_MAP.md'])
                        if 'PROJECT_MAP.md' in self._changed_paths(staged=True) and 'PROJECT_MAP.md' not in safe_paths:
                            safe_paths.append('PROJECT_MAP.md')
            self._run(["commit", "-m", message.strip()])
        except (ToolError, OSError, ValueError):
            reset_paths = list(dict.fromkeys([*safe_paths, *(['PROJECT_MAP.md'] if map_target else [])]))
            if snapshot["head"] is None:
                self._run(["rm", "--cached", "-q", "-f", "--", *reset_paths], check=False)
            else:
                self._run(["reset", "-q", "HEAD", "--", *reset_paths], check=False)
            if map_written:
                if previous_map is None:
                    map_target.unlink()
                else:
                    map_target.write_bytes(previous_map)
            raise
        # The commit succeeded. Failure to describe it must not invite a retry.
        commit, summary, remaining, warnings = None, None, None, []
        try:
            commit = self._run(["rev-parse", "HEAD"])["stdout"].strip()
            summary = self._run(["show", "--stat", "--oneline", "--no-renames",
                                 "--format=%h %s", "HEAD"])["stdout"]
            remaining = self.git_status()["status"]
        except (ToolError, OSError):
            warnings.append("Commit created; follow-up inspection failed. Inspect git_log and git_status before retrying.")
        return {"committed": True, "commit": commit, "message": message.strip(), "paths": safe_paths,
                "summary": summary, "remaining_changes": remaining, "warnings": warnings}

    def _patch_paths(self, patch):
        if not isinstance(patch, str) or not patch.strip() or len(patch) > self.MAX_PATCH:
            raise ToolError("invalid_patch", "patch must be a non-empty unified diff no larger than 200000 characters.")
        if "GIT binary patch" in patch or "Binary files " in patch:
            raise ToolError("invalid_patch", "Binary patches are not supported.")
        forbidden_metadata = (
            "rename from ", "rename to ", "copy from ", "copy to ",
            "old mode ", "new mode ", "deleted file mode ",
            "similarity index ", "dissimilarity index ",
            "diff --cc ", "diff --combined ", "Submodule ",
        )
        for line in patch.splitlines():
            if line.startswith(forbidden_metadata):
                raise ToolError("invalid_patch", "Patch contains an unsupported rename, copy, mode, deletion, combined, or submodule operation.")
            if line.startswith("new file mode ") and line != "new file mode 100644":
                raise ToolError("invalid_patch", "Only regular 100644 text files may be created.")
        headers = []
        section_open = False
        old_header = None
        for line in patch.splitlines():
            if line.startswith("diff --git "):
                if section_open and old_header is not None:
                    raise ToolError("invalid_patch", "Patch file section is missing a +++ header.")
                section_open = True
                old_header = None
                continue
            if not section_open:
                continue
            if line.startswith("@@"):
                if old_header is not None:
                    raise ToolError("invalid_patch", "Patch file section is missing a +++ header.")
                section_open = False
                continue
            if line.startswith("--- ") and old_header is None:
                old_header = line[4:].split("\t", 1)[0]
                continue
            if line.startswith("+++ ") and old_header is not None:
                new_header = line[4:].split("\t", 1)[0]
                headers.extend((("---", old_header), ("+++", new_header)))
                old_header = None
        if section_open and old_header is not None:
            raise ToolError("invalid_patch", "Patch file section is missing a +++ header.")
        if not headers:
            raise ToolError("invalid_patch", "Expected a diff --git section with paired --- and +++ headers.")
        paths = []
        for marker, raw_path in headers:
            if raw_path == "/dev/null":
                if marker == "+++":
                    raise ToolError("invalid_patch", "File deletion is not supported by apply_patch.")
                continue
            if raw_path.startswith(('"', "'")):
                raise ToolError("invalid_patch", "Quoted patch paths are not supported.")
            if not raw_path.startswith(("a/", "b/")):
                raise ToolError("invalid_patch", "Patch paths must start with a/ or b/.")
            normalized = PurePosixPath(raw_path[2:])
            safe_path = self._relative_path(normalized.as_posix())
            if safe_path not in paths:
                paths.append(safe_path)
        if not paths or len(paths) > self.MAX_FILES_PER_PATCH:
            raise ToolError("invalid_patch", "A patch must affect between 1 and 30 accessible files.")
        return paths

    @staticmethod
    def _reject_archive_marker(text):
        if any(marker in text for marker in ('[Archived executed payload.',
                '[Earlier successful output archived in log]', '[Diff archived after round closure;')):
            raise ToolError('archive_marker_not_source',
                'Refused to write an internal context-archive marker as source. No files changed. '
                'Read the current file; regenerate real complete code from the task instead of copying history placeholders.')

    def create_file(self, path, content):
        if not isinstance(content, str) or len(content) > self.MAX_PATCH or '\0' in content:
            raise ToolError('invalid_content', 'content must be UTF-8 text up to 200000 characters without NUL bytes.')
        self._reject_archive_marker(content)
        self._validate_source(path, content)
        target = self.policy.resolve_write(path)
        requested = self.root / self.policy.relative(path)
        if os.path.lexists(requested) or target.exists():
            raise ToolError('file_exists', 'Path already exists. Read it and use replace_text for an existing file.')
        temp_name = None
        warnings = []
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if self.policy.resolve_write(path) != target:
                raise ToolError('file_changed', 'Parent path changed during creation. Inspect it before retrying.')
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.orchestra-create-', delete=False) as temp:
                temp_name = temp.name
                temp.write(content.encode('utf-8'))
            # Publish complete contents atomically; linking fails if another writer won.
            try:
                os.link(temp_name, target)
            except FileExistsError as error:
                raise ToolError('file_exists', 'Path was created concurrently. Read it before editing.') from error
        except ToolError:
            raise
        except OSError as error:
            raise ToolError('write_failed', 'File could not be created. Check the parent directory and write permissions.') from error
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    warnings.append('Creation temporary file could not be removed.')
        relative = self.policy.relative(path).as_posix()
        return self._edit_result(relative, '', content, created=True, warnings=warnings)

    @staticmethod
    def _replacement_hint(text, old_text, new_text):
        present = bool(new_text) and new_text in text
        index = text.find(new_text) if present else -1
        if index < 0:
            anchors = [line for line in old_text.splitlines() if len(line.strip()) >= 8]
            for anchor in anchors[:5]:
                index = text.find(anchor)
                if index >= 0: break
            if index < 0 and anchors:
                # Bounded hint only. Similarity never authorizes a write.
                needle = max(anchors[:5], key=len).strip()[:240]
                candidates = text.splitlines()[:2000]
                best, score = None, 0.6
                for number, line in enumerate(candidates):
                    candidate = line.strip()[:240]
                    matcher = difflib.SequenceMatcher(None, needle, candidate, autojunk=False)
                    if matcher.quick_ratio() < score: continue
                    ratio = matcher.ratio()
                    if ratio > score: best, score = number, ratio
                if best is not None:
                    index = sum(len(line) for line in text.splitlines(keepends=True)[:best])
        details = {'new_text_present': present}
        if index >= 0:
            line_number = text[:index].count('\n') + 1
            lines = text.splitlines()
            start = max(1, line_number - 2)
            end = min(len(lines), start + 6)
            details['diagnostic'] = ('Current source candidate only; verify the intended location before editing.\n' +
                '\n'.join(f'{i+1}: ' + lines[i][:180] + (' ...' if len(lines[i])>180 else '')
                          for i in range(start-1,end)))[:1400]
            details.update(suggested_start_line=start, suggested_end_line=end)
        return details

    def replace_text(self, path, old_text, new_text, expected_sha256=None):
        if (not isinstance(old_text, str) or not old_text
                or not isinstance(new_text, str)
                or len(old_text) + len(new_text) > self.MAX_PATCH
                or '\0' in old_text or '\0' in new_text):
            raise ToolError("invalid_replacement", "Provide non-empty old_text and new_text, together at most 200000 characters.")
        self._reject_archive_marker(new_text)
        target = self.policy.resolve_write(path)
        if not target.is_file():
            raise ToolError("file_not_found", "replace_text requires an existing regular file.")
        if not target.stat().st_mode & stat.S_IWUSR:
            raise ToolError('write_failed', 'File is read-only. Ask the workspace owner to make it writable.')
        if target.stat().st_size > 2_000_000:
            raise ToolError("file_too_large", "replace_text supports files up to 2000000 bytes.")
        original = target.read_bytes()
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
                raise ToolError('invalid_arguments', 'expected_sha256 must be the content_sha256 returned by read_file.')
            if hashlib.sha256(original).hexdigest() != expected_sha256:
                raise ToolError('file_changed', 'File changed since read_file. Read the current contents before editing.')
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolError("unsupported_encoding", "replace_text requires UTF-8 text.") from error
        if "\0" in text:
            raise ToolError("binary_file", "Binary files cannot be edited with replace_text.")
        # Tool reads normalize CRLF; adapt fragments, preserving the file's bytes.
        if "\r\n" in text and "\n" not in text.replace("\r\n", ""):
            old_text = old_text.replace("\r\n", "\n").replace("\n", "\r\n")
            new_text = new_text.replace("\r\n", "\n").replace("\n", "\r\n")
        first = text.find(old_text)
        if first < 0:
            hint = self._replacement_hint(text, old_text, new_text)
            message = ('old_text does not match the current file. No write occurred. ' +
                       ('new_text is already present in the file; this may be a repeated edit, '
                        'but presence alone does not prove the intended location is correct. '
                        if hint['new_text_present'] else '') +
                       'Use the current candidate or a focused read and copy the exact fragment; never retry unchanged old_text.')
            raise ToolError('text_not_found', message,
                            content_sha256=hashlib.sha256(original).hexdigest(), **hint)
        if text.find(old_text, first + 1) >= 0:
            raise ToolError("text_not_unique", "old_text matches more than once. Include more surrounding context.")
        updated = (text[:first] + new_text + text[first + len(old_text):]).encode("utf-8")
        if updated == original:
            raise ToolError("no_change", "Replacement makes no change.")
        if len(updated) > 2_000_000:
            raise ToolError("file_too_large", "Result exceeds 2000000 bytes.")
        self._validate_source(path, updated.decode('utf-8'))
        temp_name = None
        warnings = []
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".orchestra-edit-", delete=False) as temp:
                temp_name = temp.name
                temp.write(updated)
            os.chmod(temp_name, stat.S_IMODE(target.stat().st_mode))
            if self.policy.resolve_write(path) != target or target.read_bytes() != original:
                raise ToolError("file_changed", "File changed during editing. Read its current contents before retrying.")
            os.replace(temp_name, target)
        finally:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    warnings.append('Editing temporary file could not be removed.')
        relative = self.policy.relative(path).as_posix()
        return self._edit_result(relative, text, updated.decode('utf-8'), replacements=1,
                                 content_sha256=hashlib.sha256(updated).hexdigest(), warnings=warnings)

    def _validate_source(self, path, text):
        if getattr(self, 'validate_python', False) and Path(path).suffix == '.py':
            try:
                compile(text, str(path), 'exec', dont_inherit=True)
            except SyntaxError as error:
                raise ToolError('invalid_python', 'Python syntax error; no file was changed.',
                    path=path, line=error.lineno, diagnostic=error.msg) from error

    def apply_patch(self, patch):
        paths = self._patch_paths(patch)
        self._reject_archive_marker('\n'.join(line[1:] for line in patch.splitlines()
                                               if line.startswith('+') and not line.startswith('+++')))
        options = ["apply", "--recount", "--whitespace=warn", "-"]
        checked = self._run([*options[:1], "--check", *options[1:]], input_text=patch, check=False)
        if checked['returncode']:
            diagnostic = checked['stderr'].replace(str(self.root), '<workspace>')[:1000].strip()
            raise ToolError('patch_check_failed',
                'Patch was rejected; no files changed. Prefer replace_text for an existing file or create_file for a new file. If a diff is needed, re-read the affected range.',
                diagnostic=diagnostic)
        result = self._run(options, input_text=patch)
        # The mutation has succeeded. Optional inspection must not turn it into
        # a reported failure, which could trigger a duplicate application.
        status = {"status": None, "truncated": False}
        diff, diff_truncated, warnings = None, False, []
        if self.available:
            try:
                status = self.git_status()
            except (ToolError, OSError):
                warnings.append("Patch applied; Git status could not be read.")
            try:
                diff, diff_truncated = self._bounded(self._run(
                    ["diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                     "--no-color", "--", *paths])["stdout"])
            except (ToolError, OSError):
                warnings.append("Patch applied; diff could not be read. Inspect affected files.")
        return {"applied": True, "files": paths, "files_changed": len(paths),
                "git_output": (result["stderr"] or result["stdout"]).strip(),
                "status": status["status"], "status_truncated": status["truncated"],
                "diff": diff, "diff_truncated": diff_truncated, "warnings": warnings}

    def execute(self, name, arguments, *, check_timeout=None):
        handlers = {"git_status": self.git_status, "git_diff": self.git_diff, "git_pull": self.git_pull,
                    "git_push": self.git_push,
                    "git_log": self.git_log, "git_show": self.git_show,
                    "git_create_branch": self.git_create_branch,
                    "run_check": self.run_check, "apply_patch": self.apply_patch,
                    "prepare_commit": self.prepare_commit, "create_commit": self.create_commit,
                    "replace_text": self.replace_text, "create_file": self.create_file}
        if name not in handlers:
            raise ToolError("invalid_arguments", "Unknown or unavailable repository tool.")
        validate_arguments(next(t for t in GIT_TOOLS if t['function']['name'] == name), arguments)
        local_arguments = dict(arguments)
        notes = local_arguments.pop('interface_notes', []) if name in EDIT_TOOL_NAMES else []
        try:
            if name == 'run_check' and check_timeout is not None:
                return self.run_check(**local_arguments, timeout_seconds=check_timeout)
            result = handlers[name](**local_arguments)
        except (OSError, UnicodeError) as error:
            raise ToolError('local_operation_failed', 'Local operation failed. Check file encoding, paths and permissions; inspect the files before retrying an edit.') from error

        if name in EDIT_TOOL_NAMES and result.get('applied'):
            if self.interface_memory_enabled:
                try:
                    memory=self.interface_map.after_edit(result.get('files',[]),notes)
                    result['interface_memory']=memory
                    result.setdefault('warnings',[]).extend(memory.get('warnings',[]))
                except Exception:
                    # The edit already happened; failure of derived metadata
                    # must never trigger a duplicate edit attempt.
                    result.setdefault('warnings',[]).append(
                        'Code changed, but interface memory refresh failed. Inspect current source before using old notes.')
                    result['interface_memory']={'updated':False}
            elif notes:
                result.setdefault('warnings',[]).append('Interface memory is disabled; notes were not saved.')
        return result
