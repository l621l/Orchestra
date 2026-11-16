"""Read-only project tools. No API keys or model calls live here."""
import base64
import fnmatch
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict
from threading import Lock
from functools import lru_cache
from orchestra_core.workspace.tool_errors import ToolError
from pathlib import Path

import regex as regex_engine
from orchestra_core.workspace.workspace_policy import WorkspacePolicy
from orchestra_core.workspace.tool_contracts import validate_arguments


def definition(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False}}}


FILE_TOOLS = [
    definition("list_files", "List a bounded project tree with continuation paths. Restricted paths and common dependency/build directories are excluded.",
               {"path": {"type": "string", "description": "Relative directory path; use . for the root."},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 6,
                              "description": "Recursive levels to show; default 3."},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000,
                                "description": "Maximum files and directories; default 400."}}, ["path"]),
    definition("read_file", "Read a UTF-8 file with 1-based line numbers and inclusive ranges. Defaults to 200 lines; returns whole lines within 20,000 characters and a continuation line. Returns content_sha256 for optional guarded replace_text. A single oversized line returns line_too_long; use search for a snippet.",
               {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1}}, ["path"]),
    definition("search_text", "Find literal text in accessible UTF-8 files up to 2 MB. Returns path:line snippets and a cursor over a saved snapshot (valid for up to 5 minutes, subject to eviction). Restart without a cursor for fresh results.",
               {"query": {"type": "string", "description": "Literal text to find; not a regular expression."},
                "path": {"type": "string", "description": "Relative file or directory to search; default ."},
                "file_pattern": {"type": "string", "description": "Case-sensitive glob: *.vue matches basenames at any depth; backend/**/*.cs matches root-relative paths, with ** matching zero or more directories."},
                "case_sensitive": {"type": "boolean", "description": "Default false."},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 5,
                                  "description": "Lines before and after each match; default 0."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500,
                                "description": "Maximum matching lines on this page; default 100."},
                "cursor": {"type": "string", "description": "next_cursor from the same search. Continues its saved snapshot; rerun without cursor if expired."}}, ["query"]),
    definition("search_regex", "Find a regular expression independently on each line of accessible UTF-8 files up to 2 MB. Returns path:line snippets and a cursor over a saved snapshot (up to 5 minutes, subject to eviction). Use search_text for literals.",
               {"pattern": {"type": "string", "description": "Regular expression matched independently against each line."},
                "path": {"type": "string", "description": "Relative file or directory to search; default ."},
                "file_pattern": {"type": "string", "description": "Case-sensitive glob: *.vue matches basenames at any depth; backend/**/*.cs matches root-relative paths, with ** matching zero or more directories."},
                "case_sensitive": {"type": "boolean", "description": "Default false."},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 5,
                                  "description": "Lines before and after each match; default 0."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500,
                                "description": "Maximum matching lines on this page; default 100."},
                "cursor": {"type": "string", "description": "next_cursor from the same search. Continues its saved snapshot; rerun without cursor if expired."}}, ["pattern"]),
]


class ProjectTools:
    def __init__(self, root, policy=None):
        self.policy = policy or WorkspacePolicy(root)
        self.root = self.policy.root
        self._search_snapshots = OrderedDict()
        self._snapshot_lock = Lock()

    @staticmethod
    def allowed(parts):
        return WorkspacePolicy.parts_allowed(parts)

    def resolve(self, path):
        return self.policy.resolve_read(path)

    def list_files(self, path, max_depth=3, max_entries=400):
        if type(max_depth) is not int or not 1 <= max_depth <= 6:
            raise ToolError("invalid_arguments", "max_depth must be an integer from 1 to 6.")
        if type(max_entries) is not int or not 1 <= max_entries <= 1000:
            raise ToolError("invalid_arguments", "max_entries must be an integer from 1 to 1000.")
        target = self.resolve(path)
        if not target.is_dir():
            raise ToolError("invalid_arguments", "Path is not a directory.")
        lines = []
        continuation_paths = []
        directory_count = 0
        file_count = 0
        output_characters = 0
        truncated = False
        visited = set()

        def scan(directory, depth):
            nonlocal directory_count, file_count, output_characters, truncated
            resolved = directory.resolve()
            if resolved in visited:
                return
            visited.add(resolved)
            try:
                children = []
                for child in directory.iterdir():
                    try:
                        safe_child = self.resolve(str(child.relative_to(self.root)))
                        children.append((safe_child.is_file(), child.name.lower(), safe_child))
                    except (ValueError, OSError):
                        continue
                children.sort(key=lambda item: (item[0], item[1]))
            except OSError:
                return

            for _, _, child in children:
                if len(lines) >= max_entries:
                    truncated = True
                    relative_directory = str(directory.relative_to(self.root)) or "."
                    if relative_directory not in continuation_paths:
                        continuation_paths.append(relative_directory)
                    return
                relative = str(child.relative_to(self.root))
                is_directory = child.is_dir()
                line = f"{'  ' * (depth - 1)}{child.name}{'/' if is_directory else ''}"
                added_characters = len(line) + (1 if lines else 0)
                if output_characters + added_characters > 20000:
                    truncated = True
                    if relative not in continuation_paths:
                        continuation_paths.append(relative if is_directory else str(directory.relative_to(self.root)))
                    return
                lines.append(line)
                output_characters += added_characters
                if is_directory:
                    directory_count += 1
                    if depth < max_depth:
                        scan(child, depth + 1)
                    else:
                        continuation_paths.append(relative)
                else:
                    file_count += 1

        scan(target, 1)
        return {"path": path, "tree": "\n".join(lines),
                "files": file_count, "directories": directory_count,
                "entries": file_count + directory_count,
                "max_depth": max_depth, "truncated": truncated,
                "continuation_paths": continuation_paths[:100]}

    def read_file(self, path, start_line=1, end_line=None):
        DEFAULT_LINES_PER_READ = 200
        if type(start_line) is not int or start_line < 1:
            raise ToolError("invalid_range", "start_line must be a positive integer.")
        if end_line is not None and (type(end_line) is not int or end_line < start_line):
            raise ToolError("invalid_range", "end_line must be an integer >= start_line.")
        target = self.resolve(path)
        if not target.is_file() or target.stat().st_size > 2_000_000:
            raise ToolError("invalid_arguments", "Expected a text file no larger than 2 MB.")
        try:
            raw = target.read_bytes()
            text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as error:
            raise ToolError('unsupported_encoding', 'read_file supports UTF-8 text. Use a UTF-8 source file.') from error
        if "\x00" in text:
            raise ToolError("invalid_arguments", "Binary files are not supported.")
        lines = text.splitlines()
        if start_line > max(1, len(lines)):
            raise ToolError('invalid_range', 'start_line is beyond the end of the file.',
                            total_lines=len(lines), requested_start_line=start_line)
        if end_line is None:
            end = min(
                start_line + DEFAULT_LINES_PER_READ - 1,
                len(lines),
            )
        else:
            end = min(end_line, len(lines))
        rendered = []
        characters = 0
        last_line = None
        for i in range(start_line - 1, end):
            line = f"{i + 1}: {lines[i]}"
            if characters + len(line) + bool(rendered) > 20000:
                if not rendered:
                    raise ToolError("line_too_long", "This line exceeds the read limit. Use search_text or search_regex for a bounded snippet.", line=i + 1)
                break
            characters += len(line) + bool(rendered)
            rendered.append(line)
            last_line = i + 1
        requested_end = min(end_line if end_line is not None else len(lines), len(lines))
        next_line = last_line + 1 if last_line is not None and last_line < requested_end else None
        return {"path": path, "total_lines": len(lines), "content": "\n".join(rendered),
                "content_sha256": hashlib.sha256(raw).hexdigest(),
                "returned_start_line": start_line if rendered else None,
                "returned_end_line": last_line, "next_start_line": next_line,
                "truncated": next_line is not None}

    @staticmethod
    def _match_snippet(line, position, limit=300):
        if len(line) <= limit:
            return line
        start = max(0, position - 60)
        start = min(start, max(0, len(line) - (limit - 2)))
        end = min(len(line), start + limit - 2)
        return ('…' if start else '') + line[start:end] + ('…' if end < len(line) else '')

    @staticmethod
    def matches_glob(path, pattern):
        """Basename patterns or slash-separated, root-relative path patterns."""
        if "/" not in pattern:
            return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)
        parts, patterns = path.split("/"), pattern.split("/")
        @lru_cache(None)
        def match(i, j):
            if j == len(patterns):
                return i == len(parts)
            if patterns[j] == "**":
                return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
            return i < len(parts) and fnmatch.fnmatchcase(parts[i], patterns[j]) and match(i + 1, j + 1)
        return match(0, 0)

    def _search_page(self, snapshot_id, snapshot, offset, max_results):
        blocks = snapshot["blocks"]
        if offset < 0 or offset > len(blocks):
            raise ToolError("invalid_cursor", "Invalid search offset; restart without cursor.")
        page, size, index = [], 0, offset
        while index < len(blocks) and len(page) < max_results:
            block = blocks[index]
            if size + len(block) + (2 if page else 0) > 20000:
                if not page:
                    raise ToolError("result_too_large", "Search result exceeds the page limit; narrow the path or reduce context_lines.")
                break
            size += len(block) + (2 if page else 0)
            page.append(block)
            index += 1
        has_more = index < len(blocks)
        cursor = None
        if has_more:
            raw = json.dumps({"snapshot": snapshot_id, "fingerprint": snapshot["fingerprint"], "offset": index}).encode()
            cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return {**snapshot["metadata"], "matches": "\n\n".join(page),
                "matches_returned": len(page), "next_cursor": cursor,
                "snapshot_id": snapshot_id,
                "truncated": has_more or snapshot["metadata"]["collection_truncated"]}

    def search_text(self, query, path=".", file_pattern=None,
                    case_sensitive=False, context_lines=0, max_results=100,
                    cursor=None):
        return self._search(query, path, file_pattern, case_sensitive,
                            context_lines, max_results, cursor, regex=False)

    def search_regex(self, pattern, path=".", file_pattern=None,
                     case_sensitive=False, context_lines=0, max_results=100,
                     cursor=None):
        return self._search(pattern, path, file_pattern, case_sensitive,
                            context_lines, max_results, cursor, regex=True)

    def _search(self, query, path=".", file_pattern=None,
                case_sensitive=False, context_lines=0, max_results=100,
                cursor=None, regex=False):
        if not isinstance(query, str) or not 1 <= len(query) <= 500:
            raise ToolError("invalid_arguments", "query must be a string from 1 to 500 characters.")
        if not isinstance(path, str):
            raise ToolError("invalid_arguments", "path must be a relative string.")
        if file_pattern is not None and (not isinstance(file_pattern, str)
                                         or not 1 <= len(file_pattern) <= 200):
            raise ToolError("invalid_arguments", "file_pattern must be a string from 1 to 200 characters.")
        if type(case_sensitive) is not bool:
            raise ToolError("invalid_arguments", "case_sensitive must be a boolean.")
        if type(context_lines) is not int or not 0 <= context_lines <= 5:
            raise ToolError("invalid_arguments", "context_lines must be an integer from 0 to 5.")
        if type(max_results) is not int or not 1 <= max_results <= 500:
            raise ToolError("invalid_arguments", "max_results must be an integer from 1 to 500.")
        if cursor is not None and not isinstance(cursor, str):
            raise ToolError("invalid_arguments", "cursor must be a string.")

        compiled_pattern = None
        if regex:
            try:
                flags = 0 if case_sensitive else regex_engine.IGNORECASE
                compiled_pattern = regex_engine.compile(query, flags)
            except regex_engine.error as error:
                raise ToolError("invalid_regex", "Invalid regular expression; check its syntax.") from error

        search_spec = json.dumps({"mode": "regex" if regex else "literal",
            "query": query, "path": path,
            "file_pattern": file_pattern, "case_sensitive": case_sensitive,
            "context_lines": context_lines}, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(search_spec.encode("utf-8")).hexdigest()[:16]
        if cursor:
            try:
                data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if (set(data) != {"snapshot", "fingerprint", "offset"}
                        or data["fingerprint"] != fingerprint
                        or not isinstance(data["snapshot"], str)
                        or type(data["offset"]) is not int):
                    raise ValueError
            except Exception as error:
                raise ToolError("invalid_cursor", "Cursor does not match this search; restart without cursor.") from error
            with self._snapshot_lock:
                snapshot = self._search_snapshots.get(data["snapshot"])
                if snapshot is None or time.monotonic() - snapshot["created"] > 300:
                    self._search_snapshots.pop(data["snapshot"], None)
                    raise ToolError("cursor_expired", "Search snapshot expired or was evicted; restart without cursor.")
                if snapshot["fingerprint"] != fingerprint:
                    raise ToolError("invalid_cursor", "Cursor does not match this search.")
                self._search_snapshots.move_to_end(data["snapshot"])
            return self._search_page(data["snapshot"], snapshot, data["offset"], max_results)

        target = self.resolve(path)
        def candidate_files():
            if target.is_file():
                yield target
                return
            for current, directories, filenames in os.walk(target):
                current_path = Path(current)
                relative_current = current_path.relative_to(self.root)
                directories[:] = [name for name in directories
                                  if self.allowed((*relative_current.parts, name))]
                directories.sort(key=str.lower)
                for filename in sorted(filenames, key=str.lower):
                    if self.allowed((*relative_current.parts, filename)):
                        yield current_path / filename

        needle = query if case_sensitive else query.casefold()
        per_file = {}
        stored_matches = 0
        stored_characters = 0
        total_matches = 0
        files_scanned = 0
        files_skipped = 0
        collection_truncated = False
        max_files = 5000
        max_stored_matches = 10000
        noise_names = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
        noise_suffixes = (".min.js", ".min.css", ".map")

        for candidate in candidate_files():
            try:
                relative = candidate.relative_to(self.root)
                safe = self.resolve(str(relative))
                if not safe.is_file():
                    continue
                relative_text = relative.as_posix()
                if file_pattern and not self.matches_glob(relative_text, file_pattern):
                    continue
                lower_name = safe.name.lower()
                if lower_name in noise_names or lower_name.endswith(noise_suffixes):
                    files_skipped += 1
                    continue
                if safe.stat().st_size > 2_000_000:
                    files_skipped += 1
                    continue
                if files_scanned >= max_files:
                    collection_truncated = True
                    break
                text = safe.read_text(encoding="utf-8")
                if "\x00" in text:
                    files_skipped += 1
                    continue
            except (ValueError, OSError, UnicodeError):
                files_skipped += 1
                continue

            files_scanned += 1
            lines = text.splitlines()
            file_record = None
            for line_number, line in enumerate(lines, 1):
                try:
                    if regex:
                        match = compiled_pattern.search(line, timeout=0.05)
                        match_start = match.start() if match else -1
                    else:
                        haystack = line if case_sensitive else line.casefold()
                        match_start = haystack.find(needle)
                        if match_start >= 0 and not case_sensitive:
                            # Case folding may expand characters (ß -> ss).
                            folded_length = 0
                            for original_index, character in enumerate(line):
                                folded_length += len(character.casefold())
                                if folded_length > match_start:
                                    match_start = original_index
                                    break
                    matched = match_start >= 0
                except TimeoutError as error:
                    raise ToolError("invalid_arguments", "Regular expression search timed out; simplify the pattern.") from error
                if not matched:
                    continue
                total_matches += 1
                if file_record is None:
                    file_record = per_file.setdefault(relative_text,
                                                       {"count": 0, "blocks": []})
                file_record["count"] += 1
                if stored_matches >= max_stored_matches:
                    collection_truncated = True
                    continue
                if context_lines == 0:
                    snippet = self._match_snippet(line, match_start)
                    block = f"{relative_text}:{line_number}:{snippet}"
                else:
                    start = max(1, line_number - context_lines)
                    end = min(len(lines), line_number + context_lines)
                    rendered_lines = [f"{relative_text}:{line_number}"]
                    for number in range(start, end + 1):
                        marker = ">" if number == line_number else " "
                        snippet = self._match_snippet(lines[number - 1],
                                                      match_start if number == line_number else 0)
                        rendered_lines.append(f"{marker} {number}: {snippet}")
                    block = "\n".join(rendered_lines)
                if stored_characters + len(block) > 500000:
                    collection_truncated = True
                    continue
                stored_characters += len(block)
                file_record["blocks"].append(block)
                stored_matches += 1

        ranked_files = sorted(per_file,
                              key=lambda name: (-per_file[name]["count"], name.lower()))
        ordered_blocks = []
        round_number = 0
        while len(ordered_blocks) < stored_matches:
            added = False
            for name in ranked_files:
                blocks = per_file[name]["blocks"]
                if round_number < len(blocks):
                    ordered_blocks.append(blocks[round_number])
                    added = True
            if not added:
                break
            round_number += 1

        snapshot = {"created": time.monotonic(), "fingerprint": fingerprint,
                    "blocks": ordered_blocks, "metadata": {
                        "mode": "regex" if regex else "literal", "query": query,
                        "path": path, "file_pattern": file_pattern,
                        "context_lines": context_lines, "total_matches": total_matches,
                        "files_with_matches": len(per_file), "files_scanned": files_scanned,
                        "files_skipped": files_skipped, "collection_truncated": collection_truncated}}
        snapshot_id = uuid.uuid4().hex
        with self._snapshot_lock:
            self._search_snapshots[snapshot_id] = snapshot
            while len(self._search_snapshots) > 8:
                self._search_snapshots.popitem(last=False)
        return self._search_page(snapshot_id, snapshot, 0, max_results)

    def execute(self, name, arguments):
        handlers = {"read_file": self.read_file, "list_files": self.list_files,
                    "search_text": self.search_text,
                    "search_regex": self.search_regex}
        if name not in handlers:
            raise ToolError("invalid_arguments", "Unknown or unavailable tool.")
        validate_arguments(next(t for t in FILE_TOOLS if t['function']['name'] == name), arguments)
        try:
            return handlers[name](**arguments)
        except OSError as error:
            raise ToolError('path_inaccessible', 'Path cannot be read. Check that it exists and is accessible.') from error


def parse_arguments(raw):
    try:
        arguments = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ToolError('invalid_arguments', 'Tool arguments must be valid JSON containing one object.') from error
    if not isinstance(arguments, dict):
        raise ToolError("invalid_arguments", "Tool arguments must be a JSON object.")
    return arguments
