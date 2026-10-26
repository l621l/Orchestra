"""Read-only project tools. No API keys or model calls live here."""
import base64
import fnmatch
import hashlib
import json
import os
from pathlib import Path


def definition(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False}}}


FILE_TOOLS = [
    definition("list_files", "Show a compact recursive project tree. Start near the project root with depth 3, then inspect only relevant subtrees. Hidden files, secrets, dependencies and generated build folders are excluded. Results are capped and report continuation paths when truncated.",
               {"path": {"type": "string", "description": "Relative directory path; use . for the root."},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 6,
                              "description": "Recursive levels to show; default 3."},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000,
                                "description": "Maximum files and directories; default 400."}}, ["path"]),
    definition("read_file", "Read a UTF-8 project file, optionally selecting an inclusive line range. Output is limited to 200 lines and 20000 characters.",
               {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1}}, ["path"]),
    definition("search_text", "Search project text files for a literal string and return compact path:line:text matches. Use this before paging through large files. Secrets, dependencies, generated folders, binary files and files over 2 MB are excluded.",
               {"query": {"type": "string", "description": "Literal text to find; not a regular expression."},
                "path": {"type": "string", "description": "Relative file or directory to search; default ."},
                "file_pattern": {"type": "string", "description": "Optional glob such as *.vue or backend/**/*.cs."},
                "case_sensitive": {"type": "boolean", "description": "Default false."},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 5,
                                  "description": "Lines before and after each match; default 0."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500,
                                "description": "Maximum matching lines on this page; default 100."},
                "cursor": {"type": "string", "description": "Opaque next_cursor from an earlier identical search."}}, ["query"]),
]
DELEGATE_TOOL = definition("delegate_task", "Delegate a substantial, self-contained task to a worker with its own conversation and read-only project tools. Multiple calls returned together run concurrently. The worker does not receive your conversation automatically, so include all relevant requirements. Avoid delegation for simple repository inspection that you can complete directly.",
                           {"task": {"type": "string"}}, ["task"])


class ProjectTools:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be a directory.")

    @staticmethod
    def allowed(parts):
        return all(not p.startswith(".") and p.lower() not in {
            "config.local.json", "node_modules", "venv", ".venv", "__pycache__",
            "logs", "dist", "build", "bin", "obj", ".idea", ".vs", "coverage"
        } and not p.lower().endswith((".pem", ".key", ".pfx")) for p in parts)

    def resolve(self, path):
        if not isinstance(path, str) or Path(path).is_absolute():
            raise ValueError("Use a relative project path.")
        relative = Path(path)
        if ".." in relative.parts or not self.allowed(relative.parts):
            raise ValueError("Path is not accessible.")
        target = (self.root / relative).resolve(strict=True)
        if not target.is_relative_to(self.root) or not self.allowed(target.relative_to(self.root).parts):
            raise ValueError("Path is outside the allowed project area.")
        return target

    def list_files(self, path, max_depth=3, max_entries=400):
        if type(max_depth) is not int or not 1 <= max_depth <= 6:
            raise ValueError("max_depth must be an integer from 1 to 6.")
        if type(max_entries) is not int or not 1 <= max_entries <= 1000:
            raise ValueError("max_entries must be an integer from 1 to 1000.")
        target = self.resolve(path)
        if not target.is_dir():
            raise ValueError("Path is not a directory.")
        lines = []
        continuation_paths = []
        directory_count = 0
        file_count = 0
        truncated = False
        visited = set()

        def scan(directory, depth):
            nonlocal directory_count, file_count, truncated
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
                if sum(len(existing) + 1 for existing in lines) + len(line) > 20000:
                    truncated = True
                    if relative not in continuation_paths:
                        continuation_paths.append(relative if is_directory else str(directory.relative_to(self.root)))
                    return
                lines.append(line)
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
            raise ValueError("start_line must be a positive integer.")
        if end_line is not None and (type(end_line) is not int or end_line < start_line):
            raise ValueError("end_line must be an integer >= start_line.")
        target = self.resolve(path)
        if not target.is_file() or target.stat().st_size > 2_000_000:
            raise ValueError("Expected a text file no larger than 2 MB.")
        text = target.read_text(encoding="utf-8")
        if "\x00" in text:
            raise ValueError("Binary files are not supported.")
        lines = text.splitlines()
        if end_line is None:
            end = min(
                start_line + DEFAULT_LINES_PER_READ - 1,
                len(lines),
            )
        else:
            end = min(end_line, len(lines))
        content = "\n".join(f"{i + 1}: {lines[i]}" for i in range(start_line - 1, end))
        return {"path": path, "total_lines": len(lines), "content": content[:20000],
                "truncated": len(content) > 20000 or end < min(end_line or len(lines), len(lines))}

    def search_text(self, query, path=".", file_pattern=None,
                    case_sensitive=False, context_lines=0, max_results=100,
                    cursor=None):
        if not isinstance(query, str) or not 1 <= len(query) <= 500:
            raise ValueError("query must be a string from 1 to 500 characters.")
        if not isinstance(path, str):
            raise ValueError("path must be a relative string.")
        if file_pattern is not None and (not isinstance(file_pattern, str)
                                         or not 1 <= len(file_pattern) <= 200):
            raise ValueError("file_pattern must be a string from 1 to 200 characters.")
        if type(case_sensitive) is not bool:
            raise ValueError("case_sensitive must be a boolean.")
        if type(context_lines) is not int or not 0 <= context_lines <= 5:
            raise ValueError("context_lines must be an integer from 0 to 5.")
        if type(max_results) is not int or not 1 <= max_results <= 500:
            raise ValueError("max_results must be an integer from 1 to 500.")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("cursor must be a string.")

        search_spec = json.dumps({"query": query, "path": path,
            "file_pattern": file_pattern, "case_sensitive": case_sensitive,
            "context_lines": context_lines}, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(search_spec.encode("utf-8")).hexdigest()[:16]
        offset = 0
        if cursor:
            try:
                padding = "=" * (-len(cursor) % 4)
                cursor_data = json.loads(base64.urlsafe_b64decode(cursor + padding))
                if cursor_data != {"fingerprint": fingerprint,
                                    "offset": cursor_data.get("offset")}:
                    raise ValueError
                offset = cursor_data["offset"]
                if type(offset) is not int or offset < 0:
                    raise ValueError
            except Exception as error:
                raise ValueError("Invalid cursor for this search.") from error

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
        total_matches = 0
        files_scanned = 0
        files_skipped = 0
        collection_truncated = False
        max_files = 5000
        max_stored_matches = 10000
        max_output_characters = 20000
        noise_names = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
        noise_suffixes = (".min.js", ".min.css", ".map")

        for candidate in candidate_files():
            try:
                relative = candidate.relative_to(self.root)
                safe = self.resolve(str(relative))
                if not safe.is_file():
                    continue
                relative_text = relative.as_posix()
                if file_pattern and not fnmatch.fnmatch(relative_text, file_pattern):
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
                haystack = line if case_sensitive else line.casefold()
                if needle not in haystack:
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
                    snippet = line.strip()
                    if len(snippet) > 300:
                        snippet = snippet[:299] + "…"
                    block = f"{relative_text}:{line_number}:{snippet}"
                else:
                    start = max(1, line_number - context_lines)
                    end = min(len(lines), line_number + context_lines)
                    rendered_lines = [f"{relative_text}:{line_number}"]
                    for number in range(start, end + 1):
                        marker = ">" if number == line_number else " "
                        snippet = lines[number - 1]
                        if len(snippet) > 300:
                            snippet = snippet[:299] + "…"
                        rendered_lines.append(f"{marker} {number}: {snippet}")
                    block = "\n".join(rendered_lines)
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

        page = []
        output_characters = 0
        index = offset
        while index < len(ordered_blocks) and len(page) < max_results:
            block = ordered_blocks[index]
            projected = output_characters + len(block) + (2 if page else 0)
            if projected > max_output_characters:
                break
            page.append(block)
            output_characters = projected
            index += 1

        has_more = index < len(ordered_blocks)
        next_cursor = None
        if has_more:
            raw_cursor = json.dumps({"fingerprint": fingerprint, "offset": index},
                                    separators=(",", ":")).encode("utf-8")
            next_cursor = base64.urlsafe_b64encode(raw_cursor).decode("ascii").rstrip("=")
        file_counts = [{"path": name, "matches": per_file[name]["count"]}
                       for name in ranked_files[:100]]
        return {"query": query, "path": path, "file_pattern": file_pattern,
                "context_lines": context_lines,
                "matches": "\n\n".join(page),
                "matches_returned": len(page), "total_matches": total_matches,
                "files_with_matches": len(per_file), "file_counts": file_counts,
                "files_scanned": files_scanned,
                "files_skipped": files_skipped,
                "truncated": has_more or collection_truncated,
                "collection_truncated": collection_truncated,
                "next_cursor": next_cursor}

    def execute(self, name, arguments):
        handlers = {"read_file": self.read_file, "list_files": self.list_files,
                    "search_text": self.search_text}
        if name not in handlers:
            raise ValueError("Unknown or unavailable tool.")
        return handlers[name](**arguments)


def parse_arguments(raw):
    arguments = json.loads(raw)
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    return arguments
