"""One path policy shared by read, search, Git, and patch tools."""
from pathlib import Path
from orchestra_core.workspace.tool_errors import ToolError


class WorkspacePolicy:
    FORBIDDEN_NAMES = {
        "config.local.json", "node_modules", "venv", ".venv", "__pycache__",
        "logs", "dist", "build", "bin", "obj", ".idea", ".vs", "coverage",
        ".git",
    }
    FORBIDDEN_SUFFIXES = (".pem", ".key", ".pfx")
    SAFE_DOTFILES = {".gitignore", ".gitattributes", ".editorconfig"}
    RESERVED_NAMES = {'con', 'prn', 'aux', 'nul',
                      *(f'com{i}' for i in range(1, 10)),
                      *(f'lpt{i}' for i in range(1, 10))}

    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ToolError("path_forbidden", "Workspace must be a directory.")

    @classmethod
    def parts_allowed(cls, parts):
        for part in parts:
            lowered = part.lower()
            if (':' in part or '\0' in part or part.endswith((' ', '.'))
                    or lowered.split('.', 1)[0] in cls.RESERVED_NAMES):
                return False
            if lowered in cls.FORBIDDEN_NAMES:
                return False
            if lowered.endswith(cls.FORBIDDEN_SUFFIXES):
                return False
            if part.startswith(".") and lowered not in cls.SAFE_DOTFILES:
                return False
        return True

    def relative(self, value):
        if not isinstance(value, str) or not value:
            raise ToolError("path_forbidden", "Use a non-empty relative project path.")
        relative = Path(value)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise ToolError("path_forbidden", "Use a relative project path without parent traversal.")
        if not self.parts_allowed(relative.parts):
            raise ToolError("path_forbidden", "Path is not accessible.")
        return relative

    def resolve_read(self, value):
        relative = self.relative(value)
        try:
            target = (self.root / relative).resolve(strict=True)
        except FileNotFoundError as error:
            raise ToolError("file_not_found", "Path not found. List the parent directory or check the relative path.") from error
        except OSError as error:
            raise ToolError("path_inaccessible", "Path cannot be accessed.") from error
        if not target.is_relative_to(self.root):
            raise ToolError("path_forbidden", "Path is outside the allowed project area.")
        resolved_relative = target.relative_to(self.root)
        if not self.parts_allowed(resolved_relative.parts):
            raise ToolError("path_forbidden", "Resolved path is not accessible.")
        return target

    def resolve_write(self, value):
        relative = self.relative(value)
        target = (self.root / relative).resolve(strict=False)
        if not target.is_relative_to(self.root):
            raise ToolError("path_forbidden", "Path is outside the allowed project area.")
        resolved_relative = target.relative_to(self.root)
        if not self.parts_allowed(resolved_relative.parts):
            raise ToolError("path_forbidden", "Resolved path is not accessible.")
        return target

    def allows_relative(self, value, write=False):
        try:
            (self.resolve_write if write else self.resolve_read)(value)
            return True
        except (OSError, ValueError):
            return False
