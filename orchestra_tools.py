"""Workspace tool setup and model-facing tool selection."""
import copy
import sys

from git_tools import GIT_TOOLS, GIT_TOOL_NAMES, WORKSPACE_TOOL_NAMES, RepositoryTools
from tool_contracts import EDIT_TOOL_NAMES
from tool_schema import compact_tools
from tools import FILE_TOOLS, ProjectTools


def workspace_tools(workspace, settings):
    files = ProjectTools(workspace)
    checks = {name: [sys.executable if arg == '{python}' else arg for arg in argv]
              for name, argv in settings.get('checks', {}).items()}
    repository = RepositoryTools(
        workspace, policy=files.policy, checks=checks,
        git_enabled=settings.get('tools', {}).get('git_tools_enabled', False),
    )
    repository.interface_memory_enabled = settings['execution'].get('project_map_enabled', True)
    return files, repository


def available_tools(settings, repository):
    options = settings.get('tools', {})
    result = list(FILE_TOOLS)
    for tool in GIT_TOOLS:
        name = tool['function']['name']
        if name in {'apply_patch', 'git_create_branch'}:
            continue
        if not options.get('git_tools_enabled', False) and name not in {'create_file','replace_text','run_check'}:
            continue
        if not repository.available and name not in WORKSPACE_TOOL_NAMES:
            continue
        if name in EDIT_TOOL_NAMES and not options.get('editing_enabled', True):
            continue
        if name == 'run_check' and not repository.checks:
            continue
        if name in {'git_log','git_show'} and not options.get('git_history_enabled',False):
            continue
        if name == 'prepare_commit' and not options.get('allow_git_mutations',False):
            continue
        if name in {'git_create_branch', 'create_commit', 'git_pull', 'git_push'} and not options.get('allow_git_mutations', False):
            continue
        if name == 'git_push' and not options.get('allow_git_push', True):
            continue
        if name == 'run_check':
            tool = copy.deepcopy(tool)
            tool['function']['parameters']['properties']['name']['enum'] = sorted(repository.checks)
        result.append(tool)
    return compact_tools(result)


def tool_handler(name, files, repository):
    return repository if name in GIT_TOOL_NAMES else files
